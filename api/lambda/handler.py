import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import ClientError


DYNAMO_TABLE_NAME = os.environ["TABLE_NAME"]
NEWSAPI_KEY = os.environ["NEWSAPI_KEY"]
SAGEMAKER_ENDPOINT_NAME = os.environ["SAGEMAKER_ENDPOINT_NAME"]
RECENCY_DAYS = int(os.environ.get("RECENCY_DAYS", "30"))
MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", "20"))
NEWSAPI_Q_MAX_LENGTH = 500
NEWS_KEYWORDS: list[str] = [
    # Labor / human rights
    "forced labor",
    "child labor",
    "modern slavery",
    "human trafficking",
    "sweatshop",
    "labor abuse",
    "worker exploitation",
    "unsafe working conditions",

    # Legal / regulatory / fines
    "lawsuit",
    "class action",
    "regulatory investigation",
    "regulatory probe",
    "regulatory fine",
    "OSHA violation",
    "EPA violation",
    "sanctions",

    # Supply chain disruption / operations
    "factory fire",
    "plant fire",
    "plant explosion",
    "plant closure",
    "factory closure",
    "shutdown",
    "supply chain disruption",
    "port congestion",
    "export ban",
    "import ban",

    # Product safety / recalls
    "product recall",
    "safety recall",
    "safety violation",
    "contamination",
    "toxic spill",

    # Financial distress / governance
    "fraud",
    "accounting scandal",
    "embezzlement",
    "bribery",
    "corruption",
    "whistleblower",
]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DYNAMO_TABLE_NAME)
sagemaker_runtime = boto3.client("sagemaker-runtime")


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _parse_brand_name(event: dict) -> str:
    body = event.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return ""
    elif body is None and isinstance(event, dict):
        body = event

    if not isinstance(body, dict):
        return ""

    return (
        body.get("brand_name")
        or body.get("brandName")
        or body.get("brand")
        or ""
    )


def _get_cached_result(brand_name: str):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RECENCY_DAYS)

    try:
        resp = table.get_item(Key={"BrandName": brand_name})
    except ClientError:
        return None

    item = resp.get("Item")
    if not item:
        return None

    time_str = item.get("TimeUpdated")
    if not time_str:
        return None

    try:
        updated = datetime.fromisoformat(
            time_str.replace("Z", "+00:00")
        )
    except ValueError:
        return None

    if updated < cutoff:
        return None

    risk_score = item.get("RiskScore")
    if isinstance(risk_score, Decimal):
        risk_score = float(risk_score)

    return {
        "brand_name": brand_name,
        "risk_score": risk_score,
        "last_updated": updated.isoformat(),
        "source": "cache",
    }


def _fetch_news_articles(brand_name: str):
    base_url = "https://newsapi.org/v2/everything"
    query_terms = [t for t in [brand_name] + NEWS_KEYWORDS if t]
    q = " OR ".join(query_terms)
    if len(q) > NEWSAPI_Q_MAX_LENGTH:
        # NewsAPI q param is max 500 chars; keep brand + as many keywords as fit
        q = brand_name or ""
        for kw in NEWS_KEYWORDS:
            candidate = f"{q} OR {kw}" if q else kw
            if len(candidate) <= NEWSAPI_Q_MAX_LENGTH:
                q = candidate
            else:
                break
        q = q[:NEWSAPI_Q_MAX_LENGTH] if len(q) > NEWSAPI_Q_MAX_LENGTH else q
    if not q:
        return []
    params = {
        "q": q,
        "apiKey": NEWSAPI_KEY,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": MAX_ARTICLES,
    }

    url = f"{base_url}?{urlencode(params)}"
    req = Request(url)

    with urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    articles = payload.get("articles", []) or []
    texts = []
    for a in articles:
        title = a.get("title") or ""
        description = a.get("description") or ""
        content = a.get("content") or ""
        combined = " ".join(
            t for t in [title, description, content] if t
        )
        if combined:
            texts.append(combined)

    return texts


def _call_sagemaker_model(brand_name: str, texts):
    # Combine fetched article texts into a single pseudo-"headline"
    # to match the SageMaker inference contract (expects `headline`).
    combined_text = " ".join(texts)
    payload = {
        "headline": combined_text,
    }

    response = sagemaker_runtime.invoke_endpoint(
        EndpointName=SAGEMAKER_ENDPOINT_NAME,
        ContentType="application/json",
        Body=json.dumps(payload).encode("utf-8"),
    )

    body_bytes = response["Body"].read()
    model_output = json.loads(body_bytes.decode("utf-8"))

    risk_score = model_output.get("risk_score")
    if isinstance(risk_score, str):
        try:
            risk_score = float(risk_score)
        except ValueError:
            risk_score = None

    return risk_score, model_output


def _save_result(brand_name: str, risk_score: float, model_output: dict):
    now = datetime.now(timezone.utc)

    item = {
        "BrandName": brand_name,
        "RiskScore": Decimal(str(risk_score)),
        "TimeUpdated": now.isoformat(),
        "ModelOutput": model_output,
        "Status": model_output.get("status"),
    }

    table.put_item(Item=item)

    return {
        "brand_name": brand_name,
        "risk_score": risk_score,
        "status": model_output.get("status"),
        "last_updated": now.isoformat(),
        "source": "fresh",
    }


def lambda_handler(event, context):
    brand_name = _parse_brand_name(event or {})
    if not brand_name:
        return _response(
            400,
            {"message": "brand_name is required in the request body"},
        )

    try:
        cached = _get_cached_result(brand_name)
        if cached:
            return _response(200, cached)

        texts = _fetch_news_articles(brand_name)
        if not texts:
            safe_model_output = {
                "status": "GREEN",
                "breakdown": {
                    "critical_risk": 0.0,
                    "moderate_risk": 0.0,
                    "safe": 1.0,
                },
            }
            result = _save_result(brand_name, 0.0, safe_model_output)
            return _response(200, result)

        risk_score, model_output = _call_sagemaker_model(
            brand_name, texts
        )
        if risk_score is None:
            return _response(
                502,
                {"message": "SageMaker model did not return a risk_score"},
            )

        result = _save_result(brand_name, risk_score, model_output)
        return _response(200, result)

    except Exception as exc:
        return _response(
            500,
            {"message": "Internal server error", "detail": str(exc)},
        )

