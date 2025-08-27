
# -*- coding: utf-8 -*-
"""
Unified Secrets Manager helper for SageMaker Studio/Spaces and
ShinyProxy ECS on-demand / pre-initialized apps.

This module keeps one consistent API for reading/writing a single JSON
"secret-of-secrets" (default key: "credentials") that lives at one of two
paths in AWS Secrets Manager:

  Shared (team-wide):   /aap/<team>/shared/<secret_key>
  Individual (per-user):/aap/<team>/<user>/<secret_key>

- <team> is inferred from the IAM role name. For ECS tasks launched by
  ShinyProxy, role names like "apps-<team>-dev-app-ondemand-ecs-task"
  are supported (the token after "apps-").  You can override with TEAM_NAME.
- <user> is inferred from environment variables, in this order:
  SHINYPROXY_USERNAME (preferred_username), SAGEMAKER_USER_PROFILE_NAME,
  JUPYTERHUB_USER, USER/USERNAME.  You can override by setting SHINYPROXY_USERNAME.

It works in BOTH runtimes:
  * SageMaker Studio/Spaces: uses sagemaker.session.get_execution_role()
  * ECS tasks: falls back to STS get_caller_identity() to obtain the role ARN

Public API (class methods):
    ProjectSecret.get_role_arn() -> str
    ProjectSecret.list_connections(shared: bool=False) -> dict
    ProjectSecret.get_connection(name: str, shared: bool=False) -> dict
    ProjectSecret.put_connection(name: str, secret_value: Any, shared: bool=False) -> dict
    ProjectSecret.delete_connection(name: str, shared: bool=False) -> None
    ProjectSecret.secrets_path(shared: bool=False) -> str

Example:
    from secrets_manager import ProjectSecret as SM

    # team-wide database creds (JSON object under key "pg")
    pg = SM.get_connection("pg", shared=True)

    # your own personal sandbox credentials
    SM.put_connection("sandbox", {"token":"***"}, shared=False)
    mine = SM.list_connections(shared=False)
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict

import boto3
from botocore.exceptions import ClientError

try:
    # Available inside SageMaker Studio/Spaces
    from sagemaker.session import get_execution_role as _sm_get_execution_role  # type: ignore
except Exception:  # pragma: no cover
    _sm_get_execution_role = None  # type: ignore

log = logging.getLogger(__name__)


class ProjectSecret:
    """Stateless helper with a classmethod-based API.

    Why a class with classmethods?
    - Keeps a clean namespace (ProjectSecret.*) for your package
    - Easy to subclass/override for tests if needed
    - No per-instance state is required; everything is derived at call time
    """

    # Base prefix in Secrets Manager
    PREFIX = os.getenv("AEP_SECRET_PREFIX", "/aap")

    # Name of the JSON secret that holds the map of logical connections
    DEFAULT_SECRET_KEY = os.getenv("AEP_SECRET_KEY", "credentials")

    # Environment variable names for user inference
    _USER_ENV_KEYS = (
        "SHINYPROXY_USERNAME",             # from OIDC preferred_username (ShinyProxy)
        "SAGEMAKER_USER_PROFILE_NAME",     # SageMaker Spaces
        "JUPYTERHUB_USER",
        "USER",
        "USERNAME",
    )

    @staticmethod
    def get_role_arn() -> str:
        """Return the current execution role ARN.

        - In SageMaker, use get_execution_role()
        - Elsewhere (e.g., ECS), fall back to STS caller identity
        """
        if _sm_get_execution_role is not None:
            try:
                arn = _sm_get_execution_role()
                if arn:
                    log.info("Using SageMaker execution role: %s", arn)
                    return arn  # type: ignore[return-value]
            except Exception as e:  # pragma: no cover - environment dependent
                log.debug("SageMaker get_execution_role failed: %s", e)

        arn = boto3.client("sts").get_caller_identity()["Arn"]
        log.info("Using caller identity ARN: %s", arn)
        return arn  # type: ignore[return-value]

    # --------------------------
    # Resolution helpers
    # --------------------------
    @classmethod
    def _parse_team_from_role(cls, arn: str) -> str:
        """Derive <team> from the IAM role name.

        Expected ECS role names include a token like:  apps-<team>-...
        We extract the component immediately after 'apps-'.  If we cannot
        determine a team, fall back to TEAM_NAME env or 'unknown'.
        """
        # Env override wins
        env_team = os.getenv("TEAM_NAME")
        if env_team:
            return env_team

        m = re.search(r":role/(?:service-role/)?([^/]+)$", arn)
        if not m:
            return "unknown"
        role_name = m.group(1)

        if role_name.startswith("apps-"):
            parts = role_name.split("-")
            if len(parts) >= 2:
                return parts[1]

        # Additional patterns can be added here if needed in future
        return "unknown"

    @classmethod
    def _resolve_team(cls) -> str:
        team = os.getenv("TEAM_NAME")
        if team:
            return team
        arn = cls.get_role_arn()
        team = cls._parse_team_from_role(arn)
        return team or "unknown"

    @staticmethod
    def _resolve_user() -> str:
        for key in ProjectSecret._USER_ENV_KEYS:
            v = os.getenv(key)
            if v:
                if "@" in v:
                    v = v.split("@")[0]
                return v.replace(".", "-")
        return "unknown"

    @classmethod
    def _base_path(cls, shared: bool) -> str:
        team = cls._resolve_team()
        if shared:
            return f"{cls.PREFIX}/{team}/shared"
        user = cls._resolve_user()
        return f"{cls.PREFIX}/{team}/{user}"

    @classmethod
    def _secret_path(cls, shared: bool, secret_secret: str | None = None) -> str:
        key = secret_secret or cls.DEFAULT_SECRET_KEY
        return f"{cls._base_path(shared)}/{key}"

    @staticmethod
    def _sm_client():
        return boto3.client("secretsmanager")

    # --------------------------
    # Low-level JSON read/write
    # --------------------------
    @classmethod
    def _read_secret_json(cls, secret_id: str) -> Dict[str, Any]:
        sm = cls._sm_client()
        try:
            resp = sm.get_secret_value(SecretId=secret_id)
        except ClientError as e:
            raise
        payload = resp.get("SecretString") or ""
        if not payload:
            return {}
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            log.warning("Secret %s is not valid JSON; returning empty dict", secret_id)
            return {}

    @classmethod
    def _write_secret_json(cls, secret_id: str, payload: Dict[str, Any]) -> None:
        sm = cls._sm_client()
        data = json.dumps(payload)
        try:
            sm.put_secret_value(SecretId=secret_id, SecretString=data)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                # First write for this secret ID → create then put
                sm.create_secret(Name=secret_id, SecretString=data)
            else:
                raise

    # --------------------------
    # Public API
    # --------------------------
    @classmethod
    def secrets_path(cls, shared: bool = False) -> str:
        """Return the fully-resolved Secrets Manager ID used to store the map."""
        return cls._secret_path(shared)

    @classmethod
    def list_connections(cls, shared: bool = False) -> Dict[str, Any]:
        """Return the whole connections map under the secret-of-secrets.

        If the secret does not exist yet, an empty dict is returned.
        """
        secret_id = cls._secret_path(shared)
        try:
            return cls._read_secret_json(secret_id)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return {}
            raise

    @classmethod
    def get_connection(cls, name: str, shared: bool = False) -> Dict[str, Any]:
        """Return one logical connection by name (e.g., 'pg')."""
        conns = cls.list_connections(shared)
        if name not in conns:
            raise KeyError(f"Connection '{name}' not found under {cls._secret_path(shared)}")
        return conns[name]

    @classmethod
    def put_connection(cls, name: str, secret_value: Any, shared: bool = False) -> Dict[str, Any]:
        """Upsert one logical connection by name."""
        secret_id = cls._secret_path(shared)
        conns = cls.list_connections(shared)
        conns[name] = secret_value
        cls._write_secret_json(secret_id, conns)
        return conns[name]

    @classmethod
    def delete_connection(cls, name: str, shared: bool = False) -> None:
        """Delete a logical connection by name (no-op if not present)."""
        secret_id = cls._secret_path(shared)
        conns = cls.list_connections(shared)
        if name in conns:
            conns.pop(name)
            cls._write_secret_json(secret_id, conns)
