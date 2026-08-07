import os
from fastapi import FastAPI
from pydantic import BaseModel
from openai import OpenAI

app = FastAPI(title="IT Support AI API")

client = OpenAI(
    base_url=os.getenv("MODEL_BASE_URL", "http://localhost:1234/v1"),
    api_key=os.getenv("MODEL_API_KEY", "lm-studio"),
)


class Ticket(BaseModel):
    title: str
    description: str
    domain: str


@app.get("/")
def root():
    return {"message": "IT Support AI API is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyze")
def analyze(ticket: Ticket):
    prompt = f"""
You are an IT operations expert.

Ticket Title:
{ticket.title}

Ticket Domain:
{ticket.domain}

Ticket Details:
{ticket.description}

Return your answer in this exact structure:

Classification:
- <short classification>

Probable Causes:
- <cause 1>
- <cause 2>
- <cause 3>

Next Checks:
- <check 1>
- <check 2>
- <check 3>
"""

    response = client.chat.completions.create(
        model=os.getenv("PRIMARY_CHAT_MODEL", "gemma-3-4b-it"),
        messages=[
            {"role": "system", "content": "You are an IT operations assistant. Be concise, structured, and practical."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.2
    )

    return {"analysis": response.choices[0].message.content}