import os, asyncio, tempfile, shutil, json, re
from pathlib import Path
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from telethon import TelegramClient, StringSession
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse, Response
from starlette.routing import Route
from starlette.middleware.cors import CORSMiddleware
import uvicorn

BOT_TOKEN=os.environ['BOT_TOKEN']
API_ID=int(os.environ['TELEGRAM_API_ID'])
API_HASH=os.environ['TELEGRAM_API_HASH']
PORT=int(os.environ.get('PORT','10000'))
BASE=os.environ.get('RENDER_EXTERNAL_URL','').rstrip('/')
WEBHOOK_PATH=os.environ.get('WEBHOOK_PATH','telegram-webhook')
STORAGE=int(os.environ.get('STORAGE_CHANNEL_ID','-1004492199475'))
SESSION=os.environ.get('TELETHON_SESSION','')
WORK=Path('/tmp/h3lium_free'); WORK.mkdir(parents=True,exist_ok=True)
telethon=None; app_tg=None; last_storage=None; processing=set(); lock=asyncio.Lock()
MANIFEST='H3LIUM_HLS_MANIFEST:'


def iid(x):
    try:return int(x)
    except:return None

def url(p): return f'{BASE}{p}' if BASE else p

def is_video(m):
    return bool(m and (m.video or (m.document and str(m.document.mime_type or '').startswith('video/'))))

def info(m):
    media=m.document or m.video
    return int(getattr(media,'size',0) or 0), getattr(media,'mime_type',None) or 'video/mp4'

async def getmsg(mid):
    m=await telethon.get_messages(STORAGE,ids=mid)
    if not m or not is_video(m): raise RuntimeError(f'Storage message {mid} is not a video.')
    return m

async def tg_start():
    global telethon
    telethon=TelegramClient(StringSession(SESSION),API_ID,API_HASH)
    if SESSION: await telethon.start()
    else: await telethon.start(bot_token=BOT_TOKEN)
    print('Telethon connected:',telethon.is_connected())

async def find_manifest(mid):
    src=await telethon.get_messages(STORAGE,ids=mid)
    if not src:return None
    cap=getattr(src,'message','') or ''
    m=re.search(re.escape(MANIFEST)+r'(\d+)',cap)
    if m:return int(m.group(1))
    ids=list(range(mid+1,min(mid+301,mid+1+300)))
    for i in range(0,len(ids),100):
        for x in await telethon.get_messages(STORAGE,ids=ids[i:i+100]):
            if x and (getattr(x,'message','') or '').startswith(MANIFEST+str(mid)): return int(x.id)
    return None

async def read_manifest(mid):
    mid=iid(mid)
    if not mid:return None
    manid=await find_manifest(mid)
    if not manid:return None
    msg=await telethon.get_messages(STORAGE,ids=manid)
    if not msg:return None
    d=WORK/str(mid); d.mkdir(parents=True,exist_ok=True); p=d/'manifest.json'
    got=await telethon.download_media(msg,file=str(p))
    if not got or not p.exists():return None
    try:return json.loads(p.read_text())
    finally:
        try:p.unlink()
        except:pass

async def tag_source(mid,manid):
    try:
        src=await telethon.get_messages(STORAGE,ids=mid)
        old=getattr(src,'message','') or ''
        if MANIFEST not in old:
            await telethon.edit_message(STORAGE,mid,text=(old+'\n\n' if old else '')+MANIFEST+str(manid))
    except Exception as e: print('manifest tag failed:',repr(e))

async def upload_segments(mid,hls,uploaded,stop):
    while not stop.is_set():
        for p in sorted(hls.glob('segment_*.ts')):
            if p.name in uploaded: continue
            try:
                s1=p.stat().st_size; await asyncio.sleep(.8)
                if not p.exists() or p.stat().st_size!=s1 or s1==0: continue
                m=await telethon.send_file(STORAGE,str(p),caption=f'H3LIUM_HLS_SEGMENT:{mid}:{p.name}',force_document=True)
                uploaded[p.name]=int(m.id); print('Uploaded',p.name,'->',m.id)
                try:p.unlink()
                except:pass
            except Exception as e: print('segment upload error',p.name,repr(e))
        await asyncio.sleep(.8)

async def ffmpeg_job(mid):
    mid=iid(mid)
    if not mid: raise RuntimeError('Invalid storage message ID')
    async with lock:
        if mid in processing: raise RuntimeError('This lecture is already processing')
        processing.add(mid)
    root=WORK/str(mid); root.mkdir(parents=True,exist_ok=True)
    hls=root/'hls'; hls.mkdir(exist_ok=True)
    srcfile=root/'input.mp4'; uploaded={}; stop=asyncio.Event(); watcher=None
    try:
        old=await read_manifest(mid)
        if old:return old
        src=await getmsg(mid); size,_=info(src)
        print(f'Downloading source {mid}, {size} bytes')
        got=await telethon.download_media(src,file=str(srcfile))
        if not got or not srcfile.exists():raise RuntimeError('Telegram download failed')
        playlist=hls/'index.m3u8'
        watcher=asyncio.create_task(upload_segments(mid,hls,uploaded,stop))
        cmd=['ffmpeg','-y','-i',str(srcfile),'-c:v','libx264','-preset','veryfast','-profile:v','main','-pix_fmt','yuv420p','-c:a','aac','-b:a','128k','-f','hls','-hls_time','10','-hls_playlist_type','vod','-hls_flags','independent_segments','-hls_segment_filename',str(hls/'segment_%05d.ts'),str(playlist)]
        print('RUN:', ' '.join(cmd))
        p=await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
        while True:
            line=await p.stderr.readline()
            if not line:break
            t=line.decode(errors='replace').strip()
            if t:print('[ffmpeg]',t)
        rc=await p.wait()
        if rc!=0:raise RuntimeError(f'FFmpeg exited with code {rc}')
        for _ in range(30):
            left=[x for x in hls.glob('segment_*.ts') if x.name not in uploaded]
            if not left:break
            await asyncio.sleep(1)
        stop.set()
        if watcher:await watcher
        for pth in sorted(hls.glob('segment_*.ts')):
            if pth.name not in uploaded:
                m=await telethon.send_file(STORAGE,str(pth),caption=f'H3LIUM_HLS_SEGMENT:{mid}:{pth.name}',force_document=True)
                uploaded[pth.name]=int(m.id); pth.unlink(missing_ok=True)
        if not playlist.exists():raise RuntimeError('FFmpeg did not create index.m3u8')
        lines=[]
        for line in playlist.read_text(errors='replace').splitlines():
            s=line.strip()
            if re.fullmatch(r'segment_\d{5}\.ts',s):lines.append(url(f'/hls/{mid}/{s}'))
            else:lines.append(line)
        man={'version':1,'source_message_id':mid,'segments':uploaded,'playlist_lines':lines,'segment_seconds':10}
        mp=root/'manifest-upload.json'; mp.write_text(json.dumps(man,separators=(',',':')))
        mm=await telethon.send_file(STORAGE,str(mp),caption=MANIFEST+str(mid),force_document=True)
        man['manifest_message_id']=int(mm.id); await tag_source(mid,int(mm.id))
        print('HLS COMPLETE',mid,'segments=',len(uploaded),'manifest=',mm.id)
        return man
    finally:
        stop.set()
        if watcher:
            try:await watcher
            except:pass
        shutil.rmtree(root,ignore_errors=True)
        async with lock:processing.discard(mid)


def parse_range(v,size):
    if not v or not v.startswith('bytes='):return None
    s=v[6:].split(',',1)[0]
    if '-' not in s:return None
    a,b=s.split('-',1)
    try:
        if not a:
            n=int(b); start=max(0,size-n); end=size-1
        else:
            start=int(a); end=int(b) if b else size-1
        if start<0 or start>=size:return None
        end=min(end,size-1)
        return start,end
    except:return None

async def video(request):
    mid=iid(request.path_params['message_id'])
    try:
        m=await getmsg(mid); size,mime=info(m); r=parse_range(request.headers.get('range'),size)
        h={'Accept-Ranges':'bytes','Access-Control-Allow-Origin':'*','Content-Type':mime,'Cache-Control':'public,max-age=3600'}
        if not r:
            h['Content-Length']=str(size); start,end=0,size-1; code=200
        else:
            start,end=r; h['Content-Length']=str(end-start+1); h['Content-Range']=f'bytes {start}-{end}/{size}'; code=206
        async def gen():
            left=end-start+1
            async for c in telethon.iter_download(m.media,offset=start,request_size=512*1024):
                if not c:break
                if len(c)>left:c=c[:left]
                yield c; left-=len(c)
                if left<=0:break
        return StreamingResponse(gen(),status_code=code,headers=h)
    except Exception as e:return PlainTextResponse(str(e),status_code=404)

async def playlist(request):
    mid=iid(request.path_params['message_id'])
    man=await read_manifest(mid)
    if not man:return JSONResponse({'status':'not_ready','message_id':mid,'direct_url':url(f'/video/{mid}')},status_code=404)
    return Response('\n'.join(man['playlist_lines'])+'\n',media_type='application/vnd.apple.mpegurl',headers={'Access-Control-Allow-Origin':'*','Cache-Control':'public,max-age=30'})

async def segment(request):
    mid=iid(request.path_params['message_id']); fn=request.path_params['filename']
    if not re.fullmatch(r'segment_\d{5}\.ts',fn):return PlainTextResponse('Invalid segment',status_code=400)
    man=await read_manifest(mid); tid=(man or {}).get('segments',{}).get(fn)
    if not tid:return PlainTextResponse('Segment not found',status_code=404)
    m=await telethon.get_messages(STORAGE,ids=int(tid))
    if not m or not m.media:return PlainTextResponse('Telegram segment unavailable',status_code=404)
    size,_=info(m)
    async def gen():
        async for c in telethon.iter_download(m.media,request_size=512*1024):
            if c:yield c
    return StreamingResponse(gen(),media_type='video/mp2t',headers={'Content-Length':str(size),'Access-Control-Allow-Origin':'*','Cache-Control':'public,max-age=86400'})

async def health(request):return JSONResponse({'status':'ok','bot':'H3LIUM Lecture Bot','telethon_connected':bool(telethon and telethon.is_connected()),'storage_channel':STORAGE,'last_storage_message_id':last_storage,'processing_ids':sorted(processing),'mode':'free-telegram-backed-hls'})

async def hls_status(request):
    mid=iid(request.path_params['message_id']); man=await read_manifest(mid)
    if not man:return JSONResponse({'status':'not_ready','message_id':mid,'playlist':url(f'/hls/{mid}/index.m3u8'),'direct':url(f'/video/{mid}')},status_code=404)
    return JSONResponse({'status':'ready','message_id':mid,'segments':len(man['segments']),'playlist':url(f'/hls/{mid}/index.m3u8'),'direct':url(f'/video/{mid}'),'manifest_message_id':man.get('manifest_message_id')})

async def start(update,context):
    await update.message.reply_text('👋 H3LIUM Lecture Bot\n\nVideo bhejo → Storage Channel.\n/process <storage_message_id> → Telegram-backed HLS.\n\nDirect fallback: /video/<id>')

async def ffmpeg_cmd(update,context):
    p=await asyncio.create_subprocess_exec('ffmpeg','-version',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE);o,_=await p.communicate()
    await update.message.reply_text('✅ '+(o.decode(errors='replace').splitlines()[0] if p.returncode==0 else '❌ FFmpeg unavailable'))

async def process(update,context):
    mid=iid(context.args[0]) if context.args else last_storage
    if not mid:return await update.message.reply_text('❌ ID do. Example: /process 19')
    if mid in processing:return await update.message.reply_text('⏳ Already processing')
    await update.message.reply_text(f'🚀 HLS started for storage message {mid}. Telegram will be permanent storage.')
    async def job():
        try:
            man=await ffmpeg_job(mid)
            await update.message.reply_text(f'🎉 HLS READY\n\nSegments: {len(man["segments"])}\n\nPlaylist:\n{url(f"/hls/{mid}/index.m3u8")}\n\nDirect:\n{url(f"/video/{mid}")}')
        except Exception as e:
            print('HLS JOB ERROR:',repr(e)); await update.message.reply_text(f'❌ HLS failed: {e}\n\nOriginal video Telegram mein safe hai. /process {mid} se retry karo.')
    context.application.create_task(job())

async def incoming(update,context):
    global last_storage
    m=update.effective_message
    if not m or (m.chat and m.chat.type==ChatType.CHANNEL) or not is_video(m):return
    try:
        c=await context.bot.copy_message(chat_id=STORAGE,from_chat_id=m.chat_id,message_id=m.message_id);last_storage=int(c.message_id)
        await m.reply_text(f'✅ Video Storage Channel mein save ho gaya.\n\n🆔 {last_storage}\n\n/process {last_storage}')
    except Exception as e:await m.reply_text(f'❌ Storage error: {e}')

async def webhook(request):
    try:
        data=await request.json(); u=Update.de_json(data=data,bot=app_tg.bot); await app_tg.process_update(u); return PlainTextResponse('OK')
    except Exception as e:print('webhook error',repr(e)); return PlainTextResponse('error',status_code=500)

async def startup():
    global app_tg
    await tg_start(); app_tg=ApplicationBuilder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler('start',start));app_tg.add_handler(CommandHandler('ffmpeg',ffmpeg_cmd));app_tg.add_handler(CommandHandler('process',process));app_tg.add_handler(MessageHandler(filters.VIDEO|filters.Document.VIDEO,incoming))
    await app_tg.initialize();await app_tg.start();await app_tg.bot.set_webhook(url(f'/{WEBHOOK_PATH}'),drop_pending_updates=True);print('Webhook:',url('/'+WEBHOOK_PATH))

async def shutdown():
    if app_tg:
        await app_tg.stop();await app_tg.shutdown()
    if telethon:await telethon.disconnect()

async def lifespan(app):await startup();yield;await shutdown()

web=Starlette(routes=[Route('/health',health),Route('/hls-status/{message_id:int}',hls_status),Route('/hls/{message_id:int}/index.m3u8',playlist),Route('/hls/{message_id:int}/{filename}',segment),Route('/video/{message_id:int}',video,methods=['GET','HEAD']),Route('/'+WEBHOOK_PATH,webhook,methods=['POST'])],lifespan=lifespan)
web.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])

if __name__=='__main__':uvicorn.run(web,host='0.0.0.0',port=PORT)
