from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import boto3
import edge_tts
import httpx
from botocore.config import Config as BotoConfig
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from PIL import Image, ImageDraw, ImageFont, ImageStat
from pydantic import BaseModel, EmailStr, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

# Logger is configured to never receive secret values (API keys, passwords, tokens) — see auth
# section below. Only non-sensitive operational messages are logged.
logger = logging.getLogger("ai_movie_studio")

ROOT = Path(__file__).resolve().parents[1]
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", ROOT / "media")).resolve()
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'movie_studio.db'}")
if DATABASE_URL.startswith("postgres://"):  # Neon/Supabase/Render give this scheme; SQLAlchemy needs postgresql://
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase): pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    projects: Mapped[list["Project"]] = relationship(back_populates="owner", cascade="all, delete-orphan")

class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    owner: Mapped[User] = relationship(back_populates="projects")
    title: Mapped[str] = mapped_column(String(200))
    idea: Mapped[str] = mapped_column(Text)
    genre: Mapped[str] = mapped_column(String(80))
    style: Mapped[str] = mapped_column(String(80))
    duration: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    stage: Mapped[str] = mapped_column(String(200), default="في قائمة الانتظار")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    script: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    scenes: Mapped[list["Scene"]] = relationship(back_populates="project", cascade="all, delete-orphan", order_by="Scene.position")

class Scene(Base):
    __tablename__ = "scenes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    position: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    narration: Mapped[str] = mapped_column(Text)
    image_prompt: Mapped[str] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    audio_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    project: Mapped[Project] = relationship(back_populates="scenes")

Base.metadata.create_all(engine)

class CreateProject(BaseModel):
    idea: str = Field(min_length=10, max_length=5000)
    genre: str = Field(default="مغامرة", max_length=80)
    style: Literal["3D", "كرتون", "أنمي", "واقعي"] = "3D"
    duration: Literal[30, 60, 90, 120] = 60

def db_session():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# --- Authentication (JWT) ---------------------------------------------------
# JWT_SECRET must come from the environment in production. A random fallback is generated
# per-process only so local dev doesn't crash without a .env file; it invalidates all
# outstanding tokens on every restart, which is intentional (never hardcode a real secret here).
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    JWT_SECRET = uuid.uuid4().hex + uuid.uuid4().hex
    logger.warning("JWT_SECRET is not set in the environment; using a temporary random secret. "
                    "Set JWT_SECRET in your .env for production so sessions survive restarts.")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)

def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": user_id, "exp": expire}, JWT_SECRET, algorithm=JWT_ALGORITHM)

# <img>/<video> tags can't send an Authorization header, so media URLs carry their own token
# in the query string. To limit the blast radius if that token leaks (browser history, proxy
# or access logs, Referer headers), it is scoped to "media" only and expires quickly — it
# cannot be used to call any other endpoint, unlike the main session token.
MEDIA_TOKEN_EXPIRE_MINUTES = 20

def create_media_token(user_id: str) -> str:
    # Bucketing the expiry to a fixed window means repeated calls within that window produce
    # the exact same token string. The frontend polls every few seconds; without this, each
    # poll would mint a new token, change the <video>/<img> src, and restart playback.
    now_minutes = int(datetime.now(timezone.utc).timestamp() // 60)
    bucket_start = now_minutes - (now_minutes % MEDIA_TOKEN_EXPIRE_MINUTES)
    expire = datetime.fromtimestamp((bucket_start + MEDIA_TOKEN_EXPIRE_MINUTES) * 60, tz=timezone.utc)
    return jwt.encode({"sub": user_id, "scope": "media", "exp": expire}, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_user_id(raw_token: str, *, require_scope: str | None = None) -> str:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "بيانات الدخول غير صالحة أو منتهية الصلاحية",
                                  headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(raw_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise unauthorized
    user_id = payload.get("sub")
    if not user_id: raise unauthorized
    if require_scope is not None and payload.get("scope") != require_scope: raise unauthorized
    return user_id

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict

def user_data(u: User) -> dict:
    return {"id": u.id, "email": u.email, "created_at": u.created_at.isoformat()}

def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(db_session),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "بيانات الدخول غير صالحة أو منتهية الصلاحية",
                             headers={"WWW-Authenticate": "Bearer"})
    user_id = decode_user_id(credentials.credentials)
    user = db.get(User, user_id)
    if not user: raise HTTPException(status.HTTP_401_UNAUTHORIZED, "بيانات الدخول غير صالحة أو منتهية الصلاحية")
    return user

def media_key(path: Path, project_id: str) -> str:
    return f"{project_id}/{path.name}"

# --- Cloudflare R2 (S3-compatible) storage, used automatically when configured ---
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET = os.getenv("R2_BUCKET")
USE_R2 = all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET])

_r2_client = None
def r2_client():
    global _r2_client
    if _r2_client is None:
        _r2_client = boto3.client("s3", endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID, aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            config=BotoConfig(signature_version="s3v4"), region_name="auto")
    return _r2_client

def store_file(path: Path, project_id: str) -> str:
    """Uploads to R2 when configured (persists independent of the server's disk) or keeps the file
    on local disk for local dev. Either way, returns only the storage *key* — never a public URL.
    Files are private: they're served through the authenticated /api/media endpoint, which checks
    project ownership before streaming (local) or issuing a short-lived presigned URL (R2)."""
    key = media_key(path, project_id)
    if USE_R2:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        r2_client().upload_file(str(path), R2_BUCKET, key, ExtraArgs={"ContentType": content_type})
    return key

def update(project_id: str, progress: int, stage: str, **fields):
    with SessionLocal() as db:
        project = db.get(Project, project_id)
        if not project: return
        project.progress, project.stage = progress, stage
        for key, value in fields.items(): setattr(project, key, value)
        db.commit()

def clean_json(value: str) -> dict:
    value = re.sub(r"^```(?:json)?|```$", "", value.strip(), flags=re.MULTILINE).strip()
    return json.loads(value[value.find("{"):value.rfind("}") + 1])

def storyboard_prompt(idea: str, genre: str, style: str, duration: int) -> str:
    scenes = max(3, min(8, round(duration / 15)))
    return f'''أنت كاتب ومخرج محترف. حوّل الفكرة إلى قصة فيلم عربية قابلة للإنتاج.
الفكرة: {idea}\nالنوع: {genre}\nالأسلوب البصري: {style}\nالمدة: {duration} ثانية.
أعد JSON فقط: {{"title":"", "script":"", "characters":[{{"name":"","appearance":"وصف بصري ثابت ودقيق"}}], "scenes":[{{"description":"", "narration":"نص عربي قصير لهذا المشهد", "image_prompt":"English cinematic image prompt"}}]}}.
أنشئ بالضبط {scenes} مشاهد. لا تذكر أسماء علامات تجارية، واجعل كل image_prompt يصف الإضاءة والتكوين والحركة، ويستعمل نفس الشخصية ووصفها في كل المشاهد.'''

def fallback_story(idea: str, genre: str, style: str, duration: int) -> dict:
    n = max(3, min(8, round(duration / 15)))
    character = {"name": "البطل", "appearance": "friendly young Arab explorer, warm brown eyes, short dark hair, teal jacket, small canvas backpack"}
    return {"title": "رحلة من فكرة إلى فيلم", "script": idea, "characters": [character], "scenes": [
        {"description": f"الفصل {i}: تتطور أحداث {idea}", "narration": f"في المشهد {i} تبدأ رحلة جديدة، وتكشف القصة خطوة مهمة نحو النهاية.", "image_prompt": f"{style} cinematic film still, {character['appearance']}, scene {i}, expressive storytelling, detailed environment, soft dramatic lighting, 16:9, no text, no watermark"}
        for i in range(1, n + 1)]}

async def generate_story(idea: str, genre: str, style: str, duration: int) -> dict:
    prompt = storyboard_prompt(idea, genre, style, duration)
    key = os.getenv("OPENAI_API_KEY")
    if key:
        try:
            from openai import OpenAI
            response = await asyncio.to_thread(OpenAI(api_key=key).chat.completions.create, model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), messages=[{"role":"user", "content":prompt}], response_format={"type":"json_object"})
            return clean_json(response.choices[0].message.content or "{}")
        except Exception: pass
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate"), json={"model":os.getenv("OLLAMA_MODEL", "llama3.2"), "prompt":prompt, "format":"json", "stream":False})
            response.raise_for_status()
            return clean_json(response.json()["response"])
    except Exception:
        return fallback_story(idea, genre, style, duration)

def valid_image(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            stat = ImageStat.Stat(image.convert("RGB").resize((64, 64)))
            return path.stat().st_size > 3000 and max(stat.mean) > 8 and (max(stat.mean) - min(stat.mean) > 2)
    except Exception: return False

def make_preview(prompt: str, target: Path, style: str):
    # A reliable non-black preview keeps the pipeline usable until an image provider is configured.
    palettes = {"أنمي": (85, 57, 164), "واقعي": (22, 58, 79), "كرتون": (238, 112, 65), "3D": (35, 93, 173)}
    base = palettes.get(style, (35, 93, 173)); image = Image.new("RGB", (1280, 720), base); draw = ImageDraw.Draw(image)
    for y in range(720):
        factor = y / 720; draw.line((0, y, 1280, y), fill=tuple(int(c * (1 - .55 * factor) + 12 * factor) for c in base))
    draw.ellipse((440, 120, 840, 520), fill=(245, 199, 132), outline=(255, 239, 190), width=10)
    draw.rectangle((350, 470, 930, 720), fill=(35, 48, 72)); draw.text((55, 55), "AI MOVIE STUDIO • PREVIEW", fill="white")
    draw.text((55, 620), prompt[:100], fill="white")
    image.save(target, quality=92)

def image_for_scene(prompt: str, target: Path, style: str):
    # Hook for Stable Diffusion/remote image APIs. The safe preview is intentional fallback, never a blank frame.
    make_preview(prompt, target, style)
    if not valid_image(target): raise RuntimeError("تعذر التحقق من الصورة الناتجة؛ أوقفنا التصدير لتفادي إطار أسود.")

async def narration_audio(text: str, target: Path):
    try:
        await edge_tts.Communicate(text=text, voice=os.getenv("TTS_VOICE", "ar-SA-HamedNeural")).save(str(target))
        if not target.exists() or target.stat().st_size < 1000: raise RuntimeError("ملف صوت غير صالح")
    except Exception as exc: raise RuntimeError(f"تعذر إنشاء التعليق الصوتي: {exc}")

def render_video(images: list[Path], audio: Path, target: Path):
    try:
        from moviepy import AudioFileClip, ImageClip, concatenate_videoclips
        from moviepy.video.fx import Resize
        narration = AudioFileClip(str(audio)); per_scene = max(2, narration.duration / len(images))
        clips = [ImageClip(str(image)).with_duration(per_scene).with_effects([Resize((1280, 720))]) for image in images]
        movie = concatenate_videoclips(clips, method="compose").with_audio(narration)
        movie.write_videofile(str(target), fps=24, codec="libx264", audio_codec="aac", logger=None)
        movie.close(); narration.close()
    except Exception as exc: raise RuntimeError(f"تعذر تركيب الفيديو. تأكد من وجود FFmpeg: {exc}")

async def build_project(project_id: str):
    try:
        with SessionLocal() as db:
            p = db.get(Project, project_id)
            if not p: return
            idea, genre, style, duration = p.idea, p.genre, p.style, p.duration
        update(project_id, 8, "نكتب السيناريو ونخطط اللقطات", status="processing")
        plan = await generate_story(idea, genre, style, duration)
        scenes = plan.get("scenes") or fallback_story(idea, genre, style, duration)["scenes"]
        characters = plan.get("characters", [])
        shared = "; ".join(str(c.get("appearance", "")) for c in characters)
        project_dir = MEDIA_ROOT / project_id; project_dir.mkdir(exist_ok=True)
        with SessionLocal() as db:
            p = db.get(Project, project_id); p.title = plan.get("title") or p.title; p.script = plan.get("script") or idea
            for i, raw in enumerate(scenes, 1):
                prompt = f"{raw.get('image_prompt', raw.get('description', ''))}. Character continuity reference: {shared}. no text, no watermark, no black frame"
                db.add(Scene(project_id=project_id, position=i, description=raw.get("description", ""), narration=raw.get("narration", ""), image_prompt=prompt))
            db.commit()
        images, narration_parts = [], []
        with SessionLocal() as db: scene_rows = db.query(Scene).filter_by(project_id=project_id).order_by(Scene.position).all()
        for i, scene in enumerate(scene_rows, 1):
            update(project_id, 15 + int(i / len(scene_rows) * 45), f"ننشئ صورة المشهد {i} من {len(scene_rows)}")
            image_path = project_dir / f"scene-{i:02d}.jpg"; image_for_scene(scene.image_prompt, image_path, style)
            images.append(image_path); narration_parts.append(scene.narration)
            with SessionLocal() as db:
                row = db.get(Scene, scene.id); row.image_url = store_file(image_path, project_id); db.commit()
        update(project_id, 67, "ننشئ تعليقًا صوتيًا طبيعيًا متصلًا")
        audio_path = project_dir / "narration.mp3"; await narration_audio(" ".join(narration_parts), audio_path)
        update(project_id, 82, "نركب الفيديو النهائي بجودة عالية")
        video_path = project_dir / "movie.mp4"; await asyncio.to_thread(render_video, images, audio_path, video_path)
        update(project_id, 100, "اكتمل الفيلم", status="complete", video_url=store_file(video_path, project_id))
    except Exception as exc:
        update(project_id, 100, "توقفت المهمة ويمكن مراجعة الخطأ", status="failed", error=str(exc))

def media_url(key: str | None, media_token: str) -> str | None:
    if not key: return None
    return f"/api/media/{key}?token={media_token}"

def scene_data(scene: Scene, media_token: str):
    return {"id":scene.id,"position":scene.position,"description":scene.description,"narration":scene.narration,"image_prompt":scene.image_prompt,"image_url":media_url(scene.image_url, media_token),"audio_url":media_url(scene.audio_url, media_token)}

def project_data(p: Project, media_token: str | None = None):
    media_token = media_token or create_media_token(p.user_id)
    return {"id":p.id,"title":p.title,"idea":p.idea,"genre":p.genre,"style":p.style,"duration":p.duration,"status":p.status,"progress":p.progress,"stage":p.stage,"error":p.error,"script":p.script,"video_url":media_url(p.video_url, media_token),"created_at":p.created_at.isoformat(),"scenes":[scene_data(s, media_token) for s in p.scenes]}

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="AI Movie Studio", version="2.1.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=1000)  # compresses JSON/video responses over ~1KB

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

@app.get("/api/health")
def health(): return {"status":"online"}  # No filesystem paths exposed publicly.

@app.get("/api/media/{project_id}/{filename}")
def get_media(project_id: str, filename: str, request: Request, token: str | None = None, db: Session = Depends(db_session)):
    """Serves a project's generated image/audio/video. Requires a valid token — either the normal
    session token (Authorization header, used by JS fetches) or a short-lived media-scoped token
    (?token=, used by <img>/<video> src attributes, which cannot set headers) — and only if that
    token's user owns the project. This is what makes user media private instead of a public URL."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        user_id = decode_user_id(auth_header.split(" ", 1)[1])
    elif token:
        user_id = decode_user_id(token, require_scope="media")
    else:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "مطلوب تسجيل الدخول للوصول إلى هذا الملف")

    project = db.get(Project, project_id)
    if not project or project.user_id != user_id: raise HTTPException(404, "الملف غير موجود")
    # filename comes straight from the URL path, so reject anything that could escape the
    # project's own media directory (path traversal) before touching the filesystem.
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise HTTPException(400, "اسم ملف غير صالح")
    key = f"{project_id}/{filename}"

    if USE_R2:
        url = r2_client().generate_presigned_url("get_object", Params={"Bucket": R2_BUCKET, "Key": key}, ExpiresIn=300)
        return RedirectResponse(url)

    file_path = (MEDIA_ROOT / project_id / filename).resolve()
    if MEDIA_ROOT.resolve() not in file_path.parents or not file_path.is_file():
        raise HTTPException(404, "الملف غير موجود")
    return FileResponse(file_path)

# --- Auth endpoints ----------------------------------------------------------
# Tight rate limits on auth endpoints specifically guard against credential
# stuffing / brute force, on top of the general limit applied below.
@app.post("/api/auth/register", status_code=201, response_model=TokenResponse)
@limiter.limit("5/minute")
def register(request: Request, body: RegisterRequest, db: Session = Depends(db_session)):
    existing = db.query(User).filter(User.email == body.email.lower()).first()
    if existing: raise HTTPException(409, "هذا البريد الإلكتروني مسجل بالفعل")
    user = User(email=body.email.lower(), password_hash=hash_password(body.password))
    db.add(user); db.commit(); db.refresh(user)
    return TokenResponse(access_token=create_access_token(user.id), user=user_data(user))

@app.post("/api/auth/login", response_model=TokenResponse)
@limiter.limit("10/minute")
def login(request: Request, body: LoginRequest, db: Session = Depends(db_session)):
    generic_error = HTTPException(401, "البريد الإلكتروني أو كلمة المرور غير صحيحة")
    user = db.query(User).filter(User.email == body.email.lower()).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise generic_error  # Same message for both cases so we don't reveal which emails are registered.
    return TokenResponse(access_token=create_access_token(user.id), user=user_data(user))

@app.get("/api/auth/me", response_model=dict)
def me(current_user: User = Depends(get_current_user)): return user_data(current_user)

# --- Project endpoints (all require a valid session; results are scoped to the caller) --------
@app.get("/api/projects")
@limiter.limit("60/minute")
def list_projects(request: Request, limit: int = 100, db: Session = Depends(db_session), current_user: User = Depends(get_current_user)):
    limit = max(1, min(limit, 200))  # guard against a caller requesting an unbounded/huge page
    return [project_data(p) for p in db.query(Project).filter_by(user_id=current_user.id).order_by(Project.created_at.desc()).limit(limit).all()]

@app.get("/api/projects/{project_id}")
@limiter.limit("60/minute")
def get_project(request: Request, project_id: str, db: Session = Depends(db_session), current_user: User = Depends(get_current_user)):
    p = db.get(Project, project_id)
    if not p or p.user_id != current_user.id: raise HTTPException(404, "المشروع غير موجود")
    return project_data(p)

@app.post("/api/projects", status_code=202)
@limiter.limit("10/minute")
async def create_project(request: Request, body: CreateProject, background: BackgroundTasks, db: Session = Depends(db_session), current_user: User = Depends(get_current_user)):
    project = Project(user_id=current_user.id, title="فيلم جديد", idea=body.idea.strip(), genre=body.genre, style=body.style, duration=body.duration)
    db.add(project); db.commit(); db.refresh(project); background.add_task(build_project, project.id)
    return project_data(project)

@app.post("/api/projects/{project_id}/retry", status_code=202)
@limiter.limit("10/minute")
async def retry(request: Request, project_id: str, background: BackgroundTasks, db: Session = Depends(db_session), current_user: User = Depends(get_current_user)):
    p = db.get(Project, project_id)
    if not p or p.user_id != current_user.id: raise HTTPException(404, "المشروع غير موجود")
    if p.status == "processing": raise HTTPException(409, "المشروع قيد الإنشاء بالفعل")
    p.status, p.error, p.progress, p.stage = "queued", None, 0, "أعيدت المهمة إلى قائمة الانتظار"; db.commit(); background.add_task(build_project, project_id)
    return project_data(p)
