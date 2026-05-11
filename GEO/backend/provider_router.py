from pydantic import BaseModel


class ProviderRequest(BaseModel):
    provider: str
    prompt: str
    model: str | None = None


SUPPORTED_PROVIDERS = [
    "openai",
    "gemini",
    "deepseek",
]


def validate_provider(provider: str):
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported provider: {provider}"
        )
