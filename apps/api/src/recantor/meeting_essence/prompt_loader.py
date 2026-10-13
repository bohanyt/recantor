from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib import resources

from recantor.meeting_essence.identities import PACKAGE_IDENTITIES, PromptIdentity


@dataclass(frozen=True)
class LoadedPrompt:
    identity: PromptIdentity
    text: str
    sha256: str


@dataclass(frozen=True)
class PromptBundle:
    package_id: str
    schema_identity: str
    algorithm_identity: str
    prompts: tuple[LoadedPrompt, ...]

    @property
    def cache_key_material(self) -> str:
        parts = [
            self.package_id,
            self.schema_identity,
            self.algorithm_identity,
            *[
                f"{item.identity.name}:{item.identity.version}:{item.sha256}"
                for item in self.prompts
            ],
        ]
        return "|".join(parts)

    @property
    def cache_key(self) -> str:
        return hashlib.sha256(self.cache_key_material.encode("utf-8")).hexdigest()


def _read_asset(relative_path: str) -> str:
    package = resources.files("recantor.meeting_essence").joinpath("assets")
    return package.joinpath(relative_path).read_text(encoding="utf-8")


def load_prompt_bundle() -> PromptBundle:
    loaded: list[LoadedPrompt] = []
    for identity in PACKAGE_IDENTITIES.prompts:
        text = _read_asset(identity.asset_path)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        loaded.append(LoadedPrompt(identity=identity, text=text, sha256=digest))
    return PromptBundle(
        package_id=PACKAGE_IDENTITIES.package_id,
        schema_identity=PACKAGE_IDENTITIES.schema_identity,
        algorithm_identity=PACKAGE_IDENTITIES.algorithm_identity,
        prompts=tuple(loaded),
    )
