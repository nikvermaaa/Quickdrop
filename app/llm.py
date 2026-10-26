import asyncio
import logging
import re
from typing import Optional, Tuple, Dict, Any
from app.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are QuickDrop's professional and empathetic Rider Support Assistant on WhatsApp.
Your job is to communicate payout, trip details, and dispute audit findings to delivery riders in a clear, polite, and helpful manner.
Respond in the language used by the rider (English or natural Hinglish).

NON-NEGOTIABLE FINANCIAL SAFETY RULES:
1. PRESERVE ALL NUMBERS & IDENTIFIERS: You MUST keep the exact rupee amounts (e.g. ₹25, ₹150, ₹10, ₹229), trip IDs (e.g. T926334, T262030), trip counts (e.g. 5 orders, 12 orders), and dates (e.g. 20 Sep, 14 Sep, 21 Sep) given in the factual summary or base wording. NEVER round, alter, or omit them.
2. NO EXTRA FINANCIAL PROMISES: Never promise money that was not approved by the system.
3. TONE: Warm, clear, respectful Indian delivery support tone (use 'aap', 'bhai' in Hinglish). Keep responses concise (1-2 sentences).
"""

def _call_gemini_sync(api_key: str, model_name: str, prompt: str) -> str:
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.2,
            ),
        )
        if response and response.text:
            return response.text.strip()
    except Exception as sdk_err:
        logger.debug(f"Gemini SDK call failed ({sdk_err}), trying REST API...")

    # Direct REST fallback via httpx
    try:
        import httpx
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        }
        with httpx.Client(timeout=10.0) as client:
            res = client.post(url, json=payload)
            if res.status_code == 200:
                data = res.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", "").strip()
    except Exception as rest_err:
        logger.warning(f"Gemini REST API fallback error: {rest_err}")

    return ""

async def _call_groq_async(api_key: str, model_name: str, prompt: str) -> str:
    try:
        from groq import AsyncGroq

        client = AsyncGroq(api_key=api_key)
        chat_completion = await client.chat.completions.create(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            model=model_name,
            temperature=0.2,
        )
        if chat_completion.choices and chat_completion.choices[0].message.content:
            return chat_completion.choices[0].message.content.strip()
    except Exception as sdk_err:
        logger.debug(f"Groq SDK call failed ({sdk_err}), trying REST API...")

    # Direct REST fallback via httpx
    try:
        import httpx
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(url, json=payload, headers=headers)
            if res.status_code == 200:
                data = res.json()
                choices = data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "").strip()
    except Exception as rest_err:
        logger.warning(f"Groq REST API fallback error: {rest_err}")

    return ""

async def generate_response_with_llm(
    rider_id: str,
    rider_message: str,
    factual_summary: str,
    default_reply: str,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Synthesizes an empathetic Hinglish/English reply using Gemini or Groq when API keys are configured.
    Falls back gracefully to default_reply if in offline mode or if API errors/rate-limits occur.
    Returns: (reply_text, trace_info_dict or None)
    """
    provider = settings.LLM_PROVIDER.lower()
    
    # Offline mode: immediate deterministic return
    if provider in ("offline", "none", "disabled", "false"):
        return default_reply, None

    prompt = (
        f"Rider {rider_id} says: \"{rider_message}\"\n\n"
        f"Factual Audit / Context:\n{factual_summary}\n\n"
        f"Base Wording:\n\"{default_reply}\"\n\n"
        f"Compose the final reply in the rider's language (English or polite Hinglish) preserving all exact amounts, order IDs, and numbers."
    )

    generated_text = ""
    model_used = ""

    # 1. Google Gemini
    if provider == "gemini" and settings.GEMINI_API_KEY:
        model_used = settings.GEMINI_MODEL
        try:
            generated_text = await asyncio.to_thread(
                _call_gemini_sync,
                settings.GEMINI_API_KEY,
                model_used,
                prompt,
            )
        except Exception as e:
            logger.warning(f"Gemini API call failed ({e}), falling back to deterministic template.")
            return default_reply, None

    # 2. Groq
    elif provider == "groq" and settings.GROQ_API_KEY:
        model_used = settings.GROQ_MODEL
        try:
            generated_text = await _call_groq_async(
                settings.GROQ_API_KEY,
                model_used,
                prompt,
            )
        except Exception as e:
            logger.warning(f"Groq API call failed ({e}), falling back to deterministic template.")
            return default_reply, None

    # If no provider matched or no API key was configured
    if not generated_text:
        return default_reply, None

    # Quality Guardrail: ensure essential numbers from default_reply weren't accidentally omitted
    numbers_in_base = re.findall(r"\b\d+\b", default_reply)
    missing_numbers = [num for num in numbers_in_base if num not in generated_text]
    if missing_numbers:
        logger.info(f"LLM omitted essential numbers {missing_numbers}; using verified base template.")
        return default_reply, None

    trace_info = {
        "input": {
            "provider": provider,
            "model": model_used,
            "prompt_length": len(prompt),
        },
        "output": {
            "generated_reply": generated_text,
            "status": "success",
        },
    }

    return generated_text, trace_info


CLASSIFY_PROMPT = """You are QuickDrop's intent classification engine for delivery rider WhatsApp messages.
Analyze the incoming rider message and return ONLY a valid JSON object (no markdown, no backticks).

Possible Intents:
1. "last_payment": Rider is asking about their past/latest payout, recent payment, money paid, or earnings (e.g. "tell me about my last payemnt", "mera last payout kitna tha", "money phir se bata dijiye", "check my payment details"). (Note: if they claim money was cut, underpaid, or missing, that is "dispute").
2. "dispute": Rider claims money is wrong, surge missing, incentive unpaid, extra penalty deducted, underpaid, etc.
3. "greeting": Rider says hi, hello, help, namaste, etc.
4. "policy_question": Rider asks how fare calculation, surge, or incentives work generally.
5. "status_check": Rider asks when money will arrive or updates ("kab tak aayega", "abhi de do").
6. "disagreement": Rider disputes facts ("dobara check karo", "nahi nahi 12 the").
7. "vague": Vague complaint without date or order ("payout galat hai").

Output format:
{
  "intent": "last_payment" | "dispute" | "greeting" | "policy_question" | "status_check" | "disagreement" | "vague",
  "topic": "incentive" | "surge" | "penalty" | null,
  "confidence": 0.0 to 1.0
}
"""

async def classify_intent_with_llm(rider_id: str, message: str) -> Optional[Dict[str, Any]]:
    """
    Uses the LLM to understand ambiguous queries, slang, and typos (e.g. 'payemnt', 'money batao').
    Returns parsed JSON dictionary or None if offline or error.
    """
    provider = settings.LLM_PROVIDER.lower()
    if provider in ("offline", "none", "disabled", "false"):
        return None

    prompt = f"Rider ID: {rider_id}\nRider Message: \"{message}\"\nReturn JSON:"
    response_text = ""

    if provider == "gemini" and settings.GEMINI_API_KEY:
        try:
            response_text = await asyncio.to_thread(
                _call_gemini_sync,
                settings.GEMINI_API_KEY,
                settings.GEMINI_MODEL,
                prompt,
            )
        except Exception as e:
            logger.debug(f"Gemini intent classification error: {e}")

    elif provider == "groq" and settings.GROQ_API_KEY:
        try:
            response_text = await _call_groq_async(
                settings.GROQ_API_KEY,
                settings.GROQ_MODEL,
                prompt,
            )
        except Exception as e:
            logger.debug(f"Groq intent classification error: {e}")

    if not response_text:
        return None

    # Parse JSON from response
    import json
    clean = re.sub(r"^```(?:json)?\s*", "", response_text.strip(), flags=re.MULTILINE)
    clean = re.sub(r"```$", "", clean.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(clean)
    except Exception:
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
    return None

