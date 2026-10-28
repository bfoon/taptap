"""Chat photos and short video clips — upload, sanitise, serve, and delete for good after a week.

Why photos are kept in the database and not on disk: web, worker and live containers all see the same
Postgres, so the hourly purge (run by Celery beat in the "live" container) can delete them without a shared
media volume. Rows are deleted outright; Postgres autovacuum hands the space back for new data.

Privacy and safety
  * The browser already re-draws the photo on a canvas (smaller, and without EXIF/GPS), but the server never
    trusts that: the file type is decided from its first bytes, and JPEG/PNG/WebP metadata blocks (EXIF,
    GPS, XMP, comments, text chunks) are cut out again here.
  * Only JPEG, PNG, WebP and GIF are accepted. SVG is refused (it can carry scripts).
  * A photo is served only to people who can open its conversation, with nosniff and a sandbox CSP.
  * View-once photos are deleted the moment someone other than the sender opens them.

Video clips (short screen recordings from a call, or a small clip sent in chat)
  * Stored in the same table as photos (mime video/webm or video/mp4), so the same hourly purge deletes them
    after CHAT_IMAGE_DAYS — every chat media file disappears after a week, no exceptions.
  * The type is decided from the first bytes (WebM/Matroska EBML header or an MP4 "ftyp" box), never from
    the file name or what the browser claims. Served with HTTP Range support so phones can seek and play.
  * CHAT_VIDEO_MAX_MB (default 16) per clip, CHAT_VIDEO_SECONDS (default 30) for call recordings,
    CHAT_VIDEO_PER_HOUR (default 20) clips per person. Clips count towards the business quota.

Space
  * CHAT_IMAGE_DAYS (default 7) — then the file is deleted for good; the message says "Photo expired".
  * CHAT_IMAGE_MAX_MB (default 5) per photo, CHAT_IMAGE_QUOTA_MB (default 300) of live photos per business,
    CHAT_IMAGE_PER_HOUR (default 60) uploads per person.
"""
from __future__ import annotations

import os
import struct
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from . import chat
from .models_chat import ChatImage, ChatMessage

DAYS = int(os.getenv('CHAT_IMAGE_DAYS', '7'))
MAX_BYTES = int(float(os.getenv('CHAT_IMAGE_MAX_MB', '5')) * 1024 * 1024)
QUOTA_BYTES = int(float(os.getenv('CHAT_IMAGE_QUOTA_MB', '300')) * 1024 * 1024)
PER_HOUR = int(os.getenv('CHAT_IMAGE_PER_HOUR', '60'))
MAX_SIDE = 8000
VIDEO_MAX_BYTES = int(float(os.getenv('CHAT_VIDEO_MAX_MB', '16')) * 1024 * 1024)
VIDEO_SECONDS = int(os.getenv('CHAT_VIDEO_SECONDS', '30'))
VIDEO_PER_HOUR = int(os.getenv('CHAT_VIDEO_PER_HOUR', '20'))
MEDIA = ('image', 'video')                      # ChatMessage.attachment values that have a ChatImage row


# ─────────────────────────────── file type ───────────────────────────────

def sniff(data: bytes) -> str:
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    return ''


def sniff_video(data: bytes) -> str:
    if data[:4] == b'\x1a\x45\xdf\xa3':          # EBML: WebM / Matroska (what Chrome, Edge and Firefox record)
        return 'video/webm'
    if data[4:8] == b'ftyp':                     # ISO base media: MP4 (what Safari records)
        return 'video/mp4'
    return ''


# ─────────────────────────────── size from the header ───────────────────────────────

def dimensions(data: bytes, mime: str):
    try:
        if mime == 'image/png':
            return struct.unpack('>II', data[16:24])
        if mime == 'image/gif':
            return struct.unpack('<HH', data[6:10])
        if mime == 'image/webp':
            kind = data[12:16]
            if kind == b'VP8X':
                w = int.from_bytes(data[24:27], 'little') + 1
                h = int.from_bytes(data[27:30], 'little') + 1
                return w, h
            if kind == b'VP8L':
                b = int.from_bytes(data[21:25], 'little')
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
            if kind == b'VP8 ':
                w, h = struct.unpack('<HH', data[26:30])
                return w & 0x3FFF, h & 0x3FFF
        if mime == 'image/jpeg':
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg = struct.unpack('>H', data[i + 2:i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack('>HH', data[i + 5:i + 9])
                    return w, h
                i += 2 + seg
    except (struct.error, IndexError):
        pass
    return 0, 0


# ─────────────────────────────── metadata stripping ───────────────────────────────

def _strip_jpeg(data: bytes) -> bytes:
    """Drop APP1..APP15 (EXIF, GPS, XMP, maker notes, Photoshop) and COM segments; keep APP0 (JFIF)
    and everything needed to decode. Stops copying segment-by-segment at the start of scan."""
    out = bytearray(b'\xff\xd8')
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return data                        # unexpected layout: keep the original rather than break it
        marker = data[i + 1]
        if marker == 0xFF:                     # fill byte
            i += 1
            continue
        if marker == 0xDA:                     # start of scan: the rest is image data
            out += data[i:]
            return bytes(out)
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            out += data[i:i + 2]
            i += 2
            continue
        seg = struct.unpack('>H', data[i + 2:i + 4])[0]
        end = i + 2 + seg
        if end > n:
            return data
        if not (0xE1 <= marker <= 0xEF or marker == 0xFE):
            out += data[i:end]
        i = end
    return data


_PNG_DROP = {b'tEXt', b'zTXt', b'iTXt', b'eXIf', b'tIME'}


def _strip_png(data: bytes) -> bytes:
    out = bytearray(data[:8])
    i = 8
    while i + 12 <= len(data):
        length = struct.unpack('>I', data[i:i + 4])[0]
        ctype = data[i + 4:i + 8]
        end = i + 12 + length
        if end > len(data):
            return data
        if ctype not in _PNG_DROP:
            out += data[i:end]
        i = end
        if ctype == b'IEND':
            break
    return bytes(out)


def _strip_webp(data: bytes) -> bytes:
    if data[12:16] != b'VP8X':
        return data                            # simple WebP files have no metadata chunks
    chunks = bytearray()
    i = 12
    while i + 8 <= len(data):
        ctype = data[i:i + 4]
        size = struct.unpack('<I', data[i + 4:i + 8])[0]
        end = i + 8 + size + (size & 1)
        if end > len(data) + 1:
            return data
        body = data[i:end]
        if ctype == b'VP8X':
            body = bytearray(body)
            body[8] &= ~(0x08 | 0x04)          # clear the EXIF and XMP flags
            body = bytes(body)
        if ctype not in (b'EXIF', b'XMP '):
            chunks += body
        i = end
    return b'RIFF' + struct.pack('<I', len(chunks) + 4) + b'WEBP' + bytes(chunks)


def strip_metadata(data: bytes, mime: str) -> bytes:
    try:
        if mime == 'image/jpeg':
            return _strip_jpeg(data)
        if mime == 'image/png':
            return _strip_png(data)
        if mime == 'image/webp':
            return _strip_webp(data)
    except (struct.error, IndexError):
        pass
    return data


# ─────────────────────────────── upload ───────────────────────────────

def business_usage(business_id) -> int:
    return ChatImage.objects.filter(business_id=business_id).aggregate(s=Sum('size'))['s'] or 0


def save_image(thread, user, upload, caption='', view_once=False):
    """Validate, clean and store a photo as a new chat message. Returns the message."""
    if upload is None:
        raise chat.ChatError('Choose a photo first.')
    if upload.size > MAX_BYTES:
        raise chat.ChatError(f'That photo is too big (max {MAX_BYTES // (1024 * 1024)} MB).')
    key = f'chat:imgrate:{user.pk}'
    n = cache.get(key, 0)
    if n >= PER_HOUR:
        raise chat.ChatError('You have sent a lot of photos this hour. Try again a little later.')
    data = upload.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise chat.ChatError('That photo is too big.')
    mime = sniff(data)
    if not mime:
        raise chat.ChatError('Only JPEG, PNG, WebP or GIF photos can be sent.')
    data = strip_metadata(data, mime)
    w, h = dimensions(data, mime)
    if not w or not h or w > MAX_SIDE or h > MAX_SIDE:
        raise chat.ChatError('That file does not look like a normal photo.')
    if QUOTA_BYTES and business_usage(thread.business_id) + len(data) > QUOTA_BYTES:
        raise chat.ChatError('Chat photo storage for this business is full. Older photos are deleted after '
                             f'{DAYS} days — try again soon.')
    view_once = bool(view_once) and thread.kind == 'direct'
    with transaction.atomic():
        m = chat.post(thread, user, caption, attachment='image')
        ChatImage.objects.create(message=m, business_id=thread.business_id, data=data, mime=mime, width=w, height=h,
                                 size=len(data), view_once=view_once,
                                 expires_at=timezone.now() + timedelta(days=DAYS))
    cache.set(key, n + 1, 3600)
    return m


def _side(v, default):
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        return default
    return v if 16 <= v <= 4096 else default


def save_video(thread, user, upload, caption='', width=0, height=0):
    """Store a short video clip (a call screen recording or a small clip) as a new chat message."""
    if upload is None:
        raise chat.ChatError('Choose a video first.')
    mb = VIDEO_MAX_BYTES // (1024 * 1024)
    if upload.size > VIDEO_MAX_BYTES:
        raise chat.ChatError(f'That video is too big (max {mb} MB). Keep clips short.')
    key = f'chat:vidrate:{user.pk}'
    n = cache.get(key, 0)
    if n >= VIDEO_PER_HOUR:
        raise chat.ChatError('You have sent a lot of videos this hour. Try again a little later.')
    data = upload.read(VIDEO_MAX_BYTES + 1)
    if len(data) > VIDEO_MAX_BYTES:
        raise chat.ChatError(f'That video is too big (max {mb} MB).')
    if len(data) < 64:
        raise chat.ChatError('That video is empty.')
    mime = sniff_video(data)
    if not mime:
        raise chat.ChatError('Only WebM or MP4 videos can be sent.')
    if QUOTA_BYTES and business_usage(thread.business_id) + len(data) > QUOTA_BYTES:
        raise chat.ChatError('Chat media storage for this business is full. Older files are deleted after '
                             f'{DAYS} days — try again soon.')
    w, h = _side(width, 1280), _side(height, 720)
    with transaction.atomic():
        m = chat.post(thread, user, caption, attachment='video')
        ChatImage.objects.create(message=m, business_id=thread.business_id, data=data, mime=mime, width=w, height=h,
                                 size=len(data), view_once=False,
                                 expires_at=timezone.now() + timedelta(days=DAYS))
    cache.set(key, n + 1, 3600)
    return m


# ─────────────────────────────── message JSON helpers ───────────────────────────────

def prime(messages):
    """Load photo details (never the bytes) for a list of messages in one query; message_json reads m._img."""
    ids = [m.pk for m in messages if m.attachment in MEDIA]
    found = {}
    if ids:
        for im in ChatImage.objects.filter(message_id__in=ids).defer('data'):
            found[im.message_id] = im
    for m in messages:
        if m.attachment in MEDIA:
            m._img = found.get(m.pk)
    return messages


def image_json(m):
    if m.attachment == 'opened':
        return {'gone': True, 'opened': True}
    if m.attachment not in MEDIA:
        return None
    kind = m.attachment
    im = getattr(m, '_img', 'unset')
    if im == 'unset':
        im = ChatImage.objects.filter(message_id=m.pk).defer('data').first()
    if im is None or im.expires_at <= timezone.now():
        return {'gone': True, 'opened': False, 'kind': kind}
    return {'gone': False, 'id': m.pk, 'kind': kind, 'w': im.width, 'h': im.height, 'once': im.view_once,
            'expires': timezone.localtime(im.expires_at).isoformat(), 'size': im.size}


# ─────────────────────────────── serving ───────────────────────────────

def open_image(message, user):
    """(bytes, mime, view_once) for someone allowed to see the conversation; deletes view-once photos
    when the other person opens them. Returns None when the photo has gone."""
    im = ChatImage.objects.filter(message=message).first()
    if im is None or im.expires_at <= timezone.now():
        return None
    data, mime, once = bytes(im.data), im.mime, im.view_once
    if once:
        if message.sender_id == user.pk:
            return 'sender'
        # first opener wins; the row is gone before the bytes even leave the server
        deleted, _ = ChatImage.objects.filter(pk=im.pk).delete()
        if not deleted:
            return None
        ChatMessage.objects.filter(pk=message.pk).update(attachment='opened')
    return data, mime, once


# ─────────────────────────────── purge ───────────────────────────────

def purge_expired(now=None):
    """Delete every chat photo and video clip past its expiry, for good. Run hourly by Celery beat."""
    now = now or timezone.now()
    total = 0
    while True:                                 # in batches so a big backlog never locks the table for long
        ids = list(ChatImage.objects.filter(expires_at__lte=now).values_list('id', flat=True)[:500])
        if not ids:
            return total
        deleted, _ = ChatImage.objects.filter(id__in=ids).delete()
        total += deleted
