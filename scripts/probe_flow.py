import asyncio, json, uuid, datetime, sys
import httpx, bcrypt
from pymongo import MongoClient

BASE="http://localhost:3080"; MONGO="mongodb://172.18.0.4:27017/LibreChat"
PW="loadlab-pass-1"; EMAIL="lt+probe@loadlab.invalid"; NO_PARENT="00000000-0000-0000-0000-000000000000"

def seed():
    db=MongoClient(MONGO).get_database()
    db.users.delete_many({"email":EMAIL})
    tmpl=db.users.find_one({"provider":"local"})
    now=datetime.datetime.now(datetime.timezone.utc)
    doc={k:v for k,v in tmpl.items() if k not in ("_id","refreshToken","backupCodes")}
    doc.update(email=EMAIL,name="loadlab probe",emailVerified=True,
               password=bcrypt.hashpw(PW.encode(),bcrypt.gensalt(10)).decode(),
               createdAt=now,updatedAt=now)
    r=db.users.insert_one(doc); return str(r.inserted_id)

async def main():
    uid=seed(); print("seeded",uid)
    async with httpx.AsyncClient(base_url=BASE,timeout=120,headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"}) as c:
        r=await c.post("/api/auth/login",json={"email":EMAIL,"password":PW})
        print("login",r.status_code,list(r.json())[:4] if r.headers.get("content-type","").startswith("application/json") else r.text[:200])
        tok=r.json()["token"]; H={"Authorization":f"Bearer {tok}","User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"}
        mid=str(uuid.uuid4())
        body={"text":"Say exactly: PONG","sender":"User","isCreatedByUser":True,
              "parentMessageId":NO_PARENT,"conversationId":"new","messageId":mid,
              "responseMessageId":mid+"_","endpoint":"local27b","endpointType":"custom",
              "model":"qwen3.6-27b","isTemporary":False,"isRegenerate":False,"error":False}
        r=await c.post("/api/agents/chat/local27b",headers=H,json=body)
        print("chat post",r.status_code,r.text[:400])
        if r.status_code!=200: return
        start=r.json(); sid=start["streamId"]; print("streamId",sid,"convo",start.get("conversationId"))
        events=[]
        async with c.stream("GET",f"/api/agents/chat/stream/{sid}?resume=true",headers=H) as s:
            print("sse status",s.status_code)
            cur={}
            async for line in s.aiter_lines():
                if line.startswith("event: "): cur["event"]=line[7:]
                elif line.startswith("data: "):
                    cur["data"]=line[6:]
                elif line=="":
                    if cur:
                        ev=cur.get("event"); d=cur.get("data","")
                        events.append(ev)
                        print("EV",ev,d[:220])
                        if ev in ("final","error","done"): break
                    cur={}
        print("events",events)

asyncio.run(main())
