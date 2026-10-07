"""Read the selected directory through current owner capabilities."""
from collections.abc import Mapping

from agent.plugin_composition import Context
from agent.plugin_composition.model import ServiceKey
from agent.plugin_composition.rpc import rpc_method_key
from agent.plugin_composition.runtime_catalog import (
    RUNTIME_CATALOG, RUNTIME_MCP_DETAIL, RuntimeCatalogUnavailable,
)

_METHODS = {
    "documents": "inspection/documents.list",
    "document": "inspection/documents.get",
    "jobs": "inspection/jobs.list",
    "job": "inspection/jobs.get",
}


class RuntimeDirectory:
    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx
        self.read = ctx.entrypoint(self._read)

    async def _rpc(self, name: str, values: dict[str, object]) -> dict[str, object]:
        with self._ctx.borrow(rpc_method_key(name)) as method:
            if method is None:
                return {"unavailable": {
                    "code": "inspection_unavailable", "message": "此目录的检查服务尚未绑定",
                }}
            result = await method.invoke(method.params.model_validate(values), None)
        if not isinstance(result, Mapping):
            raise TypeError("目录检查服务返回了无效响应")
        return dict(result)

    async def _read(self, section: str, values: dict[str, object]) -> dict[str, object]:
        if section in _METHODS:
            return await self._rpc(_METHODS[section], values)
        if section == "capabilities":
            catalog = dict(self._ctx.require(RUNTIME_CATALOG)(self._ctx))
            if "mcp_unavailable" in catalog:
                return {"unavailable": catalog["mcp_unavailable"]}
            skills = await self._rpc("inspection/skills.list", {})
            if "unavailable" in skills:
                return skills
            if not isinstance(skills.get("items"), list):
                raise TypeError("技能目录返回了无效列表")
            return {**catalog, "skills": skills["items"]}
        if section == "mcp":
            owner_id, name = values.get("owner_id"), values.get("name")
            if not isinstance(owner_id, str) or not isinstance(name, str):
                raise ValueError("MCP 详情需要 owner_id 和 name")
            try:
                tools = await self._ctx.require(RUNTIME_MCP_DETAIL)(self._ctx, owner_id, name)
            except RuntimeCatalogUnavailable as error:
                return {"unavailable": {"code": error.code, "message": str(error)}}
            return {"owner_id": owner_id, "name": name, "tools": tools, "tool_count": len(tools)}
        raise KeyError(section)


DIRECTORY = ServiceKey[RuntimeDirectory]("runtime-ui.directory.v1")
