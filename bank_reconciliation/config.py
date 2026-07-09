"""Settings loaded from .env. Credentials never live in code."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# project-root .env, resolved absolutely so cwd never matters
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    # Cosmos
    cosmos_endpoint: str = ""
    cosmos_key: str = ""
    cosmos_database: str = "reconciliation"
    cosmos_file_container: str = "bank_files"
    cosmos_sap_container: str = "bank-sap-source"   # SAP side now; swap to SAP API later
    cosmos_registry_container: str = "template_registry"
    cosmos_evidence_container: str = "evidence"
    cosmos_results_container: str = "recon_results"

    # SAP
    sap_read_mode: str = "cosmos"   # cosmos (bank-sap-source) | api
    sap_api_base: str = ""
    sap_api_token: str = ""
    # SAP write-back (mode=WRITE). OFF by default — dry-run builds the payload but never sends.
    sap_write_enabled: bool = False
    sap_write_api_base: str = ""    # falls back to sap_api_base if empty

    # Cosmos write-back defaults for run_cosmos_workflow. When true, the plain command
    # writes WITHOUT needing --write-sap/--write-summary. Pass --dry-run to force no writes.
    write_sap: bool = False
    write_summary: bool = False
    write_evidence: bool = False   # per-comparison audit trail -> evidence container

    # mode
    source_mode: str = "cosmos"  # cosmos (live) | fixture (local JSON offline test)

    # Triage / explanation agent — advisory LLM layer (see explaination_agent.md).
    # OFF by default: the deterministic pipeline runs unchanged when disabled.
    triage_enabled: bool = False
    llm_provider: str = "openai"          # openai | azure
    openai_api_key: str = ""              # env OPENAI_API_KEY
    triage_model: str = "gpt-4o-mini"     # env TRIAGE_MODEL (cheap tier is enough)
    azure_openai_endpoint: str = ""
    azure_openai_key: str = ""
    azure_openai_api_version: str = "2024-08-01-preview"


settings = Settings()
