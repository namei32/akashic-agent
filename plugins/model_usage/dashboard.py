from datetime import datetime

from fastapi import FastAPI, HTTPException, Query
from agent.plugin_composition import DashboardContext
from plugins.models.contract import MODEL_USAGE_REPORTS, ModelUsageQuery

inject = (MODEL_USAGE_REPORTS,)


def register(app: FastAPI, context: DashboardContext) -> None:
    @app.get("/api/dashboard/model-usage")
    async def report(start: datetime, end: datetime, model: str | None = Query(default=None, max_length=256),
                     cursor: str | None = Query(default=None, max_length=1024)) -> dict[str, object]:
        try:
            return await context.require(MODEL_USAGE_REPORTS).report(ModelUsageQuery(start, end, model, cursor))
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except TimeoutError as error:
            raise HTTPException(503, str(error)) from error
