from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/review")
def review_page(request: Request):
    return templates.TemplateResponse(request=request, name="review.html", context={})
