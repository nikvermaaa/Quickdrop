import asyncio
import uuid
import logging
from typing import List, Dict, Any, Optional
import httpx
from app.config import settings

logger = logging.getLogger(__name__)

class PaySwiftClient:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or settings.PAYSWIFT_BASE_URL).rstrip("/")

    async def check_health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                res = await client.get(f"{self.base_url}/health")
                return res.status_code == 200
        except Exception as e:
            logger.warning(f"PaySwift health check failed: {e}")
            return False

    async def get_payouts(self, rider_id: Optional[str] = None) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/v1/payouts"
        params = {"rider_id": rider_id} if rider_id else {}
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    res = await client.get(url, params=params)
                    if res.status_code == 200:
                        data = res.json()
                        if isinstance(data, list):
                            return data
                        return data.get("data") or data.get("payouts") or []
                    elif res.status_code in (429, 500, 502, 503, 504):
                        await asyncio.sleep(0.2 * (2 ** attempt))
                        continue
                    else:
                        logger.error(f"PaySwift GET /v1/payouts error: {res.status_code} {res.text}")
                        return []
            except Exception as e:
                logger.warning(f"PaySwift GET /v1/payouts attempt {attempt+1} failed: {e}")
                await asyncio.sleep(0.2 * (2 ** attempt))
        return []

    async def create_payout(
        self,
        rider_id: str,
        amount: int,
        reference: str,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/v1/payouts"
        key = idempotency_key or str(uuid.uuid4())
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": key,
        }
        payload = {
            "rider_id": rider_id,
            "amount": int(amount),
            "reference": str(reference),
        }

        for attempt in range(5):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    res = await client.post(url, json=payload, headers=headers)
                    if res.status_code in (200, 201):
                        return res.json()
                    elif res.status_code in (429, 500, 502, 503, 504):
                        await asyncio.sleep(0.25 * (2 ** attempt))
                        continue
                    elif res.status_code == 409:
                        return res.json()
                    else:
                        logger.error(f"PaySwift POST /v1/payouts failed {res.status_code}: {res.text}")
                        return {"error": f"HTTP {res.status_code}: {res.text}"}
            except Exception as e:
                logger.warning(f"PaySwift POST /v1/payouts attempt {attempt+1} failed: {e}")
                await asyncio.sleep(0.25 * (2 ** attempt))

        return {"error": "PaySwift service unreachable or timed out after retries"}

payswift_client = PaySwiftClient()
