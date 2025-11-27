import os

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kiki_core import llm_chat_simple, llm_ansible_ai

app = FastAPI(title="KIKI Web")

# static / templates 설정
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/chat")
async def api_chat(prompt: str = Form(...)):
    """
    일반 chat 용 API
    """
    try:
        reply = llm_chat_simple(prompt)
        return JSONResponse({"ok": True, "reply": reply})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/ansible-ai")
async def api_ansible_ai(
    prompt: str = Form(...),
    target: str = Form("ansible"),
    inventory: str = Form(""),
    verify: str = Form("none"),
):
    """
    자연어 → YAML (Ansible/K8s/OSP/Heat) API
    """
    try:
        inv = inventory.strip() or None
        yaml_text = llm_ansible_ai(prompt=prompt, target=target, inventory=inv, verify=verify)
        return JSONResponse({"ok": True, "yaml": yaml_text})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8090")), reload=True)
