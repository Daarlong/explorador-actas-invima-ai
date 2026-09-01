from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class LLMSettings:
    provider: str = "prompt_only"
    openai_api_key: str = ""
    openai_model: str = ""
    azure_api_key: str = ""
    azure_endpoint: str = ""
    azure_deployment: str = ""
    azure_api_version: str = "2024-10-21"

    @property
    def is_configured(self) -> bool:
        if self.provider == "openai":
            return bool(self.openai_api_key and self.openai_model)
        if self.provider == "azure_openai":
            return bool(
                self.azure_api_key
                and self.azure_endpoint
                and self.azure_deployment
                and self.azure_api_version
            )
        return False


def _value(source: Mapping, key: str, default: str = "") -> str:
    value = source.get(key, os.getenv(key, default))
    return str(value).strip() if value is not None else default


def load_llm_settings(source: Mapping | None = None) -> LLMSettings:
    source = source or {}
    return LLMSettings(
        provider=_value(source, "LLM_PROVIDER", "prompt_only").lower(),
        openai_api_key=_value(source, "OPENAI_API_KEY"),
        openai_model=_value(source, "OPENAI_MODEL"),
        azure_api_key=_value(source, "AZURE_OPENAI_API_KEY"),
        azure_endpoint=_value(source, "AZURE_OPENAI_ENDPOINT"),
        azure_deployment=_value(source, "AZURE_OPENAI_DEPLOYMENT"),
        azure_api_version=_value(
            source, "AZURE_OPENAI_API_VERSION", "2024-10-21"
        ),
    )


def generate_answer(prompt: str, settings: LLMSettings) -> str:
    if not settings.is_configured:
        raise RuntimeError("No hay un proveedor de IA configurado")

    try:
        from openai import AzureOpenAI, OpenAI
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise RuntimeError("La dependencia openai no está instalada") from exc

    if settings.provider == "openai":
        client = OpenAI(api_key=settings.openai_api_key)
        model = settings.openai_model
    elif settings.provider == "azure_openai":
        client = AzureOpenAI(
            api_key=settings.azure_api_key,
            azure_endpoint=settings.azure_endpoint,
            api_version=settings.azure_api_version,
        )
        model = settings.azure_deployment
    else:
        raise RuntimeError(f"Proveedor de IA no reconocido: {settings.provider}")

    response = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "Responde solo con evidencia del contexto y conserva "
                    "las citas [F#]."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("El proveedor de IA devolvió una respuesta vacía")
    return content

