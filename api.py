import rater
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pymongo import MongoClient
from pydantic import BaseModel, HttpUrl
from datetime import datetime
import os
import uuid
import httpx

collection = MongoClient(rater.CONFIG["mongo_url"])["advert_rater"]["ratings"]

class AnalysisRequest(BaseModel):
    csv_url: HttpUrl

with open("api_key", "r") as f:
    API_KEY = f.read().strip()

def cleanup(filepath: str):
    if os.path.exists(filepath):
        os.remove(filepath)

app = FastAPI()

@app.post("/analyze-minimal")
async def analyze_csv(request: AnalysisRequest, background_tasks: BackgroundTasks):
    temp_path = f"/tmp/{uuid.uuid4()}.csv"
    
    # 1. Stream download to disk (Safe for 2GB RAM)
    try:
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", str(request.csv_url)) as response:
                response.raise_for_status()
                with open(temp_path, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        f.write(chunk)
    except Exception as e:
        if os.path.exists(temp_path): os.remove(temp_path)
        raise HTTPException(status_code=400, detail=f"Download failed: {e}")
    try:
        # Register cleanup to run after the response is sent
        background_tasks.add_task(cleanup, temp_path)

        data = rater.read_csv(temp_path)
        for row in data:
            rater.rateAndSave(row['url'], collection,row['email'],row['name'])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Processing error: {e}")

@app.get("/byScore/{type}/{operation}/{value}")
async def get_by_score(type: str, operation: str, value: int):
    if type not in ["performance", "accessibility", "best-practices", "seo"]:
        raise HTTPException(status_code=400, detail="Invalid score type")
    if operation not in [">", "<", ">=", "<=", "=="]:
        raise HTTPException(status_code=400, detail="Invalid operation")
    
    try:
        query = {f"{type}": {"$" + {"<": "lt", ">": "gt", "<=": "lte", ">=": "gte", "==": "eq"}[operation]: value}}
        results = list(collection.find(query))
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database query error: {e}")