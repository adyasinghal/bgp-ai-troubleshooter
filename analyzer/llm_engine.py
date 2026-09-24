"""
LLM-Assisted Diagnosis & Explanation Layer.

Scope:
- Receives structured evidence from deterministic diagnostic tools.
- Formulates strict prompts adhering to evidence-only reasoning.
- Interprets and correlates multi-layer operational telemetry (BGP, Interface, TCP, Config).
- Produces structured JSON diagnosis with confidence, confirmed findings, likely causes,
  missing evidence, and recommended actions.
- Safe fallback: operates gracefully if disabled, unconfigured, timed out, or failing.
- Deterministic Verdict remains primary authority.
- No commands executed; no configs modified; no secrets logged.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

# Sanitization regex for stripping potential secret keys
SENSITIVE_KEY_RE = re.compile(r"(password|secret|token|api_key|auth|credential|private_key)", re.IGNORECASE)

SYSTEM_PROMPT = """You are an expert network troubleshooting assistant.
You analyze diagnostic evidence collected by deterministic network troubleshooting tools.

CRITICAL RULES:
1. Reason ONLY from the supplied structured evidence.
2. Do NOT invent device state, configuration, routes, errors, or CLI commands.
3. Distinguish clearly:
   - Confirmed finding (directly proven by evidence)
   - Likely explanation (strong logical inference from multiple facts)
   - Possible explanation (plausible hypothesis requiring more proof)
   - Missing evidence (relevant information not present in the evidence)
4. If deterministic evaluation already identifies a root cause, explain that finding clearly rather than contradicting it without evidence.
5. If evidence is insufficient or unresolved, explain what was ruled out, what remains uncertain, and what specific evidence is missing.
6. Never claim certainty when evidence does not support it. Confidence MUST be one of: "high", "medium", "low".
7. Do NOT execute commands or modify network devices.
8. When recommending an action, clearly identify it as a suggested next step for human review.
9. Return ONLY a valid JSON object matching the requested schema.

Output Schema:
{
    "summary": "Brief 1-2 sentence executive summary",
    "diagnosis": "Concise technical diagnosis",
    "confidence": "high" | "medium" | "low",
    "confirmed_findings": ["string", ...],
    "likely_causes": ["string", ...],
    "evidence": ["string", ...],
    "missing_evidence": ["string", ...],
    "recommended_actions": ["string", ...],
    "explanation": "Detailed paragraph explaining the correlation of evidence"
}
"""


@dataclass
class LLMDiagnosis:
    """Structured response from the LLM diagnosis layer."""
    summary: str
    diagnosis: str
    confidence: str = "low"  # "high", "medium", "low"
    confirmed_findings: List[str] = field(default_factory=list)
    likely_causes: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    missing_evidence: List[str] = field(default_factory=list)
    recommended_actions: List[str] = field(default_factory=list)
    explanation: str = ""
    status: str = "success"  # "success", "unavailable", "error", "disabled"
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "diagnosis": self.diagnosis,
            "confidence": self.confidence,
            "confirmed_findings": self.confirmed_findings,
            "likely_causes": self.likely_causes,
            "evidence": self.evidence,
            "missing_evidence": self.missing_evidence,
            "recommended_actions": self.recommended_actions,
            "explanation": self.explanation,
            "status": self.status,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LLMDiagnosis":
        if not isinstance(data, dict):
            return cls(
                summary="Invalid LLM response format",
                diagnosis="Unable to parse diagnosis",
                confidence="low",
                status="error",
                error="Response is not a valid dictionary",
            )

        # Normalize confidence to high/medium/low
        raw_conf = str(data.get("confidence", "medium")).lower().strip()
        if "high" in raw_conf:
            conf = "high"
        elif "low" in raw_conf:
            conf = "low"
        else:
            conf = "medium"

        return cls(
            summary=str(data.get("summary", "")).strip(),
            diagnosis=str(data.get("diagnosis", "")).strip(),
            confidence=conf,
            confirmed_findings=list(data.get("confirmed_findings") or []),
            likely_causes=list(data.get("likely_causes") or []),
            evidence=list(data.get("evidence") or []),
            missing_evidence=list(data.get("missing_evidence") or []),
            recommended_actions=list(data.get("recommended_actions") or []),
            explanation=str(data.get("explanation", "")).strip(),
            status=str(data.get("status", "success")),
            error=data.get("error"),
        )

    def pretty(self) -> str:
        lines = []
        lines.append("=" * 50)
        lines.append("LLM-ASSISTED DIAGNOSIS & EXPLANATION")
        lines.append("=" * 50)

        if self.status != "success":
            lines.append(f"Status:      {self.status.upper()}")
            if self.error:
                lines.append(f"Notice:      {self.error}")
            lines.append(f"Summary:     {self.summary}")
            return "\n".join(lines)

        lines.append(f"Confidence:  {self.confidence.upper()}")
        lines.append(f"Summary:     {self.summary}")
        lines.append(f"Diagnosis:   {self.diagnosis}")

        if self.confirmed_findings:
            lines.append("")
            lines.append("Confirmed Findings:")
            for item in self.confirmed_findings:
                lines.append(f"  - {item}")

        if self.likely_causes:
            lines.append("")
            lines.append("Likely Causes / Explanations:")
            for item in self.likely_causes:
                lines.append(f"  - {item}")

        if self.missing_evidence:
            lines.append("")
            lines.append("Missing Evidence:")
            for item in self.missing_evidence:
                lines.append(f"  - {item}")

        if self.recommended_actions:
            lines.append("")
            lines.append("Recommended Next Investigation:")
            for item in self.recommended_actions:
                lines.append(f"  - {item}")

        if self.explanation:
            lines.append("")
            lines.append("Technical Explanation:")
            lines.append(f"  {self.explanation}")

        return "\n".join(lines)


class EvidenceBuilder:
    """Prepares, sanitizes, and summarizes evidence for LLM consumption."""

    MAX_ROUTES_FOR_LLM = 10

    @classmethod
    def sanitize(cls, obj: Any) -> Any:
        """Recursively strip sensitive credential keys."""
        if isinstance(obj, dict):
            return {
                k: cls.sanitize(v)
                for k, v in obj.items()
                if not SENSITIVE_KEY_RE.search(k)
            }
        if isinstance(obj, list):
            return [cls.sanitize(item) for item in obj]
        return obj

    @classmethod
    def prepare_routes_summary(
        cls, raw_routes: Dict[str, Any], peer_ip: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Safely summarize routing telemetry without overloading context.
        Preserves peer routes, best paths, and invalid paths up to MAX_ROUTES_FOR_LLM.
        """
        if not isinstance(raw_routes, dict):
            return {"total_prefixes": 0, "routes_sample": []}

        routes_map = raw_routes.get("routes", {})
        if not isinstance(routes_map, dict):
            return {"total_prefixes": 0, "routes_sample": []}

        total_prefixes = len(routes_map)
        best_count = 0
        invalid_count = 0
        peer_matching_paths = []
        other_sample_paths = []

        for prefix, path_list in routes_map.items():
            if not isinstance(path_list, list):
                continue
            for p in path_list:
                if not isinstance(p, dict):
                    continue
                if p.get("bestpath"):
                    best_count += 1
                if p.get("valid") is False:
                    invalid_count += 1

                # Check if path relates to peer
                is_peer_path = False
                if peer_ip:
                    if p.get("peer") == peer_ip or peer_ip in (p.get("next_hops") or []):
                        is_peer_path = True

                entry = {
                    "prefix": prefix,
                    "valid": p.get("valid"),
                    "bestpath": p.get("bestpath"),
                    "peer": p.get("peer"),
                    "as_path": p.get("as_path"),
                    "next_hops": p.get("next_hops"),
                    "metric": p.get("metric"),
                }

                if is_peer_path:
                    peer_matching_paths.append(entry)
                else:
                    other_sample_paths.append(entry)

        # Select up to MAX_ROUTES_FOR_LLM paths prioritizing peer routes
        selected = peer_matching_paths[:cls.MAX_ROUTES_FOR_LLM]
        remaining = cls.MAX_ROUTES_FOR_LLM - len(selected)
        if remaining > 0:
            selected.extend(other_sample_paths[:remaining])

        summary = {
            "total_prefixes": total_prefixes,
            "best_path_count": best_count,
            "invalid_path_count": invalid_count,
            "routes_sample": selected,
        }

        if total_prefixes > cls.MAX_ROUTES_FOR_LLM or len(selected) < total_prefixes:
            summary["truncated"] = True
            summary["truncation_notice"] = (
                "Route telemetry was truncated; conclusions about routes are limited to the provided subset."
            )
        else:
            summary["truncated"] = False

        return summary

    @classmethod
    def build_context(
        cls,
        question: str,
        host: str,
        peer: Optional[str],
        starting_intent: str,
        checked: List[str],
        evidence: List[Dict[str, Any]],
        deterministic_result: Dict[str, Any],
        telemetry: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Build a sanitized, structured context object for the LLM.
        """
        structured_evidence: Dict[str, Any] = {}

        for item in evidence:
            tool_id = item.get("tool", "")
            parsed = item.get("parsed", {})
            if tool_id == "bgp_state":
                structured_evidence["bgp_summary"] = parsed
            elif tool_id == "interface":
                structured_evidence["interface"] = parsed
            elif tool_id == "tcp_port":
                structured_evidence["tcp_port"] = parsed
            elif tool_id == "config":
                structured_evidence["config"] = parsed
            else:
                structured_evidence[tool_id] = parsed

        # Enrich with deep telemetry if available
        if telemetry:
            if "neighbors" in telemetry:
                neighbors_parsed = telemetry["neighbors"].get("parsed", {})
                if isinstance(neighbors_parsed, dict) and "neighbors" in neighbors_parsed:
                    structured_evidence["bgp_neighbors"] = neighbors_parsed["neighbors"]
            if "routes" in telemetry:
                routes_parsed = telemetry["routes"].get("parsed", {})
                if isinstance(routes_parsed, dict):
                    structured_evidence["bgp_routes"] = cls.prepare_routes_summary(routes_parsed, peer)

        context = {
            "question": question,
            "host": host,
            "peer": peer,
            "starting_intent": starting_intent,
            "tools_checked": checked,
            "evidence": structured_evidence,
            "deterministic_result": deterministic_result,
        }

        return cls.sanitize(context)


class LLMEngine:
    """
    LLM diagnostic layer orchestrator with provider abstraction and safe fallback.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 10.0,
        enabled: Optional[bool] = None,
        client: Optional[Any] = None,
    ):
        if api_key is not None:
            self.api_key = api_key if api_key.strip() else None
        else:
            self.api_key = (
                os.getenv("LLM_API_KEY")
                or os.getenv("GEMINI_API_KEY")
                or os.getenv("GROQ_API_KEY")
                or os.getenv("OPENAI_API_KEY")
            )
        self.model = model or os.getenv("LLM_MODEL", "gemini-3.5-flash-lite")
        self.timeout = timeout
        self.custom_client = client

        if enabled is not None:
            self.enabled = enabled
        else:
            env_en = os.getenv("LLM_ENABLED", "false").strip().lower()
            self.enabled = env_en in ("true", "1", "yes", "on") or (self.api_key is not None or client is not None)

    def is_available(self) -> bool:
        """Check if LLM diagnosis can be performed."""
        return bool(self.enabled and (self.api_key or self.custom_client))

    def diagnose(self, context: Dict[str, Any]) -> LLMDiagnosis:
        """
        Execute LLM diagnosis on structured context.
        Always returns an LLMDiagnosis without raising exceptions.
        """
        if not self.enabled:
            return LLMDiagnosis(
                summary="LLM-assisted diagnosis is disabled.",
                diagnosis="Deterministic reasoning only.",
                confidence="low",
                status="disabled",
            )

        if not self.is_available():
            return LLMDiagnosis(
                summary="LLM diagnosis unavailable (missing API key or provider).",
                diagnosis="Deterministic reasoning only.",
                confidence="low",
                status="unavailable",
                error="No LLM_API_KEY or provider client configured.",
            )

        sanitized_context = EvidenceBuilder.sanitize(context)
        prompt = (
            f"User Question: {sanitized_context.get('question', '')}\n"
            f"Target Host: {sanitized_context.get('host', '')}\n"
            f"Target Peer: {sanitized_context.get('peer', 'None')}\n\n"
            f"Deterministic Result:\n{json.dumps(sanitized_context.get('deterministic_result', {}), indent=2)}\n\n"
            f"Structured Tool Evidence:\n{json.dumps(sanitized_context.get('evidence', {}), indent=2)}\n\n"
            "Analyze the evidence and provide structured diagnostic JSON adhering strictly to the schema."
        )

        try:
            raw_response = self._call_provider(prompt, SYSTEM_PROMPT)
            parsed_json = self._extract_and_parse_json(raw_response)
            return LLMDiagnosis.from_dict(parsed_json)
        except Exception as exc:
            # Safe fallback: never break callers
            err_msg = str(exc)
            # Ensure no API key is leaked in error message
            if self.api_key and self.api_key in err_msg:
                err_msg = err_msg.replace(self.api_key, "[REDACTED_API_KEY]")
            logger.warning(f"LLM diagnosis failed: {err_msg}")
            return LLMDiagnosis(
                summary="LLM diagnosis encountered an error; relying on deterministic result.",
                diagnosis="LLM diagnosis unavailable.",
                confidence="low",
                status="error",
                error=err_msg,
            )

    def _call_provider(self, prompt: str, system_instruction: str) -> str:
        """Dispatch call to custom mock client or standard provider."""
        if self.custom_client is not None:
            if callable(self.custom_client):
                return self.custom_client(prompt, system_instruction)
            if hasattr(self.custom_client, "generate"):
                return self.custom_client.generate(prompt, system_instruction)
            if hasattr(self.custom_client, "diagnose"):
                return self.custom_client.diagnose(prompt, system_instruction)
            raise ValueError("Provided custom_client is not callable and has no supported method.")

        # Default provider integration: Google GenAI / Gemini
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.api_key)
            response = client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                ),
            )
            if hasattr(response, "text") and response.text:
                return response.text
            return str(response)
        except ImportError:
            pass

        # Fallback to requests HTTP direct call if google-genai library is not used
        import requests
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "generationConfig": {"responseMimeType": "application/json"},
        }
        resp = requests.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            if parts:
                return parts[0].get("text", "")
        raise ValueError("No content generated in LLM response")

    def _extract_and_parse_json(self, raw_text: str) -> Dict[str, Any]:
        """Safely parse JSON from raw text, removing markdown code fences if present."""
        if not raw_text or not isinstance(raw_text, str):
            raise ValueError("Empty or non-string response from LLM")

        cleaned = raw_text.strip()
        # Remove ```json ... ``` blocks if present
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            # Attempt to find first { and last }
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(cleaned[start : end + 1])
                except Exception:
                    pass
            raise ValueError(f"Malformed JSON in LLM response: {e}")
