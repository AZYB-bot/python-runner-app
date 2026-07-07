import streamlit as st
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import random
from datetime import datetime
from io import StringIO
from collections import deque

try:
    import jmcomic
    from jmcomic import JmOption, create_option_by_str
    JMCOMIC_AVAILABLE = True
except ImportError:
    JMCOMIC_AVAILABLE = False
    st.error("⚠️ jmcomic 库未安装，请运行 `pip install jmcomic`")

st.set_page_config(page_title="JM Downloader", layout="wide")

if not JMCOMIC_AVAILABLE:
    st.stop()

OPTION_BASE = {
    "download": {
        "cache": True,
        "image": {"decode": True, "suffix": ".jpg"},
        "threading": {"image": 50, "photo": 20},
    },
    "client": {"impl": "api", "retry_times": 3, "timeout": 15},
}

_progress_queues: dict = {}
_download_tasks: dict = {}
_task_counter = 0
_task_lock = threading.Lock()

class ProgressCapture(StringIO):
    def __init__(self, q):
        super().__init__()
        self.q = q
        self._chapter_done = 0
        self._chapter_total = 0
        self._image_done = 0
        self._image_total = 0

    def write(self, s):
        super().write(s)
        line = s.strip()
        if not line:
            return
        msg = {"type": "log", "text": line}

        m = re.search(r"章节数:\s*(\d+)", line)
        if m:
            self._chapter_total = int(m.group(1))

        m = re.search(r"共\s*(\d+)\s*章节", line)
        if m:
            self._chapter_total = int(m.group(1))

        m = re.search(r"\[(\d+)/(\d+)\]", line)
        if m:
            self._image_done = int(m.group(1))
            self._image_total = int(m.group(2))

        if "章节下载完成" in line:
            self._chapter_done += 1

        if "合并PDF成功" in line:
            msg["pdf_ok"] = True

        if "本子下载完成" in line:
            m = re.search(r"\[(\d+)\]", line)
            if m:
                msg["album_done_id"] = m.group(1)

        msg["chapter_done"] = self._chapter_done
        msg["chapter_total"] = max(self._chapter_total, 1)
        msg["image_done"] = self._image_done
        msg["image_total"] = max(self._image_total, 1)

        self.q.put(msg)

def _build_option(temp_dir: str) -> JmOption:
    cfg = dict(OPTION_BASE)
    cfg["dir_rule"] = {"rule": "Bd_Aid", "base_dir": temp_dir}
    cfg["plugins"] = {
        "after_photo": [
            {
                "plugin": "img2pdf",
                "kwargs": {
                    "pdf_dir": temp_dir,
                    "filename_rule": "Pindex",
                },
            }
        ]
    }
    return JmOption.construct(cfg)

def _run_download(task_id: str, album_id: str, q: queue.Queue, temp_dir: str):
    try:
        old = sys.stdout
        cap = ProgressCapture(q)
        sys.stdout = cap
        option = _build_option(temp_dir)
        option.download_album(album_id)

        pdf_files = sorted(
            [f for f in os.listdir(temp_dir) if f.endswith(".pdf")],
            key=lambda x: int(re.sub(r"\D", "", x) or 0),
        )
        if not pdf_files:
            raise RuntimeError("未生成 PDF 文件")

        if len(pdf_files) == 1:
            final_pdf = os.path.join(temp_dir, f"{album_id}.pdf")
            os.rename(os.path.join(temp_dir, pdf_files[0]), final_pdf)
            q.put({"type": "done", "album_id": album_id, "file": final_pdf})
        else:
            q.put({"type": "done", "album_id": album_id, "files": pdf_files})
        if task_id in _download_tasks:
            _download_tasks[task_id]["status"] = "已完成"
    except Exception as e:
        q.put({"type": "error", "message": str(e)})
        if task_id in _download_tasks:
            _download_tasks[task_id]["status"] = "失败"
    finally:
        sys.stdout = old

def get_album_info(album_id: str):
    try:
        opt = create_option_by_str("log: false\nclient: {impl: api, retry_times: 3}")
        client = opt.build_jm_client()
        album = client.get_album_detail(album_id)
        episodes = album.episode_list

        authors = [a for a in album.authors if a != "N/A"]
        author = authors[0] if authors else album.author
        tags = [t for t in (album.tags or []) if t != "N/A"]

        chapters = []
        total_pages = 0
        for photo_id, index, _title in episodes:
            try:
                photo = client.get_photo_detail(photo_id)
                pages = len(photo.page_arr) if photo.page_arr else 0
                title = photo.name or _title or ""
                chapters.append({"id": photo_id, "title": title, "pages": pages})
                total_pages += pages
            except Exception:
                chapters.append({"id": photo_id, "title": _title, "pages": 0})

        return {
            "id": album.album_id,
            "title": album.name,
            "author": author,
            "tags": tags,
            "chapter_count": len(episodes),
            "page_count": total_pages,
            "chapters": chapters,
        }
    except Exception as e:
        return {"error": str(e)}

def search_tag(tag: str, page: int = 1):
    try:
        opt = create_option_by_str("log: false\nclient: {impl: api, retry_times: 3}")
        client = opt.build_jm_client()
        result = client.search_tag(tag, page=page)
        items = [{"id": aid, "title": title} for aid, title in result.iter_id_title()]
        return {"items": items, "page": page, "tag": tag}
    except Exception as e:
        return {"error": str(e)}

def random_album(tag: str = "百合"):
    try:
        opt = create_option_by_str("log: false\nclient: {impl: api, retry_times: 3}")
        client = opt.build_jm_client()
        page = random.randint(1, 30)
        items = list(client.search_tag(f"+{tag}", page=page).iter_id_title())
        if not items:
            return {"error": f"标签「{tag}」无结果"}
        album_id, title = random.choice(items)
        return {"id": album_id, "title": title, "tag": tag}
    except Exception as e:
        return {"error": str(e)}

def get_cover_image(album_id: str):
    try:
        opt = JmOption.construct({
            "log": False,
            "client": {"impl": "api", "retry_times": 5},
            "download": {"cache": False, "image": {"suffix": ".jpg"}},
        })
        client = opt.build_jm_client()
        album = client.get_album_detail(album_id)
        if not album.episode_list:
            return None

        first_photo_id = album.episode_list[0][0]
        photo = client.get_photo_detail(first_photo_id)
        if not photo.page_arr:
            return None

        img_detail = photo.create_image_detail(0)
        fd, path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        try:
            client.download_by_image_detail(img_detail, path)
            with open(path, "rb") as f:
                return f.read()
        finally:
            os.remove(path)
    except Exception:
        return None

def start_download(album_id: str):
    global _task_counter
    with _task_lock:
        _task_counter += 1
        task_id = str(_task_counter)

    _download_tasks[task_id] = {
        "album_id": album_id,
        "start_time": datetime.now(),
        "status": "下载中",
    }

    temp_dir = tempfile.mkdtemp(prefix=f"jm_{album_id}_")
    q = queue.Queue()
    _progress_queues[task_id] = (q, temp_dir, album_id)
    threading.Thread(
        target=_run_download, args=(task_id, album_id, q, temp_dir), daemon=True
    ).start()
    return task_id

def check_download_status(task_id: str):
    entry = _progress_queues.get(task_id)
    if entry is None:
        return {"error": "任务不存在"}

    q, _td, _aid = entry
    msgs = []
    while True:
        try:
            msg = q.get_nowait()
            msgs.append(msg)
            if msg.get("type") in ("done", "error"):
                break
        except queue.Empty:
            break

    last = msgs[-1] if msgs else {}
    if last.get("type") == "done":
        return {"status": "done", "task_id": task_id, "file": last.get("file"), "files": last.get("files")}
    if last.get("type") == "error":
        return {"error": last.get("message")}

    return {
        "status": "downloading",
        "text": last.get("text", ""),
        "chapter_done": last.get("chapter_done", 0),
        "chapter_total": last.get("chapter_total", 1),
        "image_done": last.get("image_done", 0),
        "image_total": last.get("image_total", 1),
    }

def get_download_file(task_id: str):
    entry = _progress_queues.get(task_id)
    if entry is None:
        return None, None

    _q, temp_dir, album_id = entry
    pdf_files = sorted(
        [f for f in os.listdir(temp_dir) if f.endswith(".pdf")],
        key=lambda x: int(re.sub(r"\D", "", x) or 0),
    )
    if not pdf_files:
        return None, None

    if len(pdf_files) == 1:
        final_pdf = os.path.join(temp_dir, pdf_files[0])
        download_name = f"{album_id}.pdf"
        mime = "application/pdf"
    else:
        import zipfile
        zip_path = os.path.join(temp_dir, f"{album_id}.zip")
        if not os.path.exists(zip_path):
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for pf in pdf_files:
                    zf.write(os.path.join(temp_dir, pf), pf)
        final_pdf = zip_path
        download_name = f"{album_id}.zip"
        mime = "application/zip"

    with open(final_pdf, "rb") as f:
        content = f.read()

    def _delayed_cleanup():
        time.sleep(600)
        _progress_queues.pop(task_id, None)
        shutil.rmtree(temp_dir, ignore_errors=True)
    threading.Thread(target=_delayed_cleanup, daemon=True).start()

    return content, download_name

st.title("📚 JM Downloader")

tab1, tab2 = st.tabs(["本子下载", "搜索"])

with tab1:
    col1, col2, col3 = st.columns([4, 2, 2])
    with col1:
        album_id = st.text_input("输入本子号", placeholder="如 422866", key="album_id")
    with col2:
        st.write("")
        st.write("")
        search_btn = st.button("查询")
    with col3:
        st.write("")
        st.write("")
        random_lily = st.button("🎲 随机百合")
        random_guro = st.button("🎲 随机猎奇")

    if random_lily:
        result = random_album("百合")
        if "error" not in result:
            st.session_state.album_id = result["id"]
            album_id = result["id"]
            st.success(f"🎲 随机百合: [{result['id']}] {result['title']}")

    if random_guro:
        result = random_album("猎奇")
        if "error" not in result:
            st.session_state.album_id = result["id"]
            album_id = result["id"]
            st.success(f"🎲 随机猎奇: [{result['id']}] {result['title']}")

    if search_btn and album_id:
        info = get_album_info(album_id)
        if "error" in info:
            st.error(f"获取失败: {info['error']}")
        else:
            st.session_state.album_info = info

    if "album_info" in st.session_state:
        info = st.session_state.album_info
        if "error" in info:
            st.error(info["error"])
        else:
            col_left, col_right = st.columns([3, 7])
            with col_left:
                cover_data = get_cover_image(info["id"])
                if cover_data:
                    st.image(cover_data, use_column_width=True)
                else:
                    st.image("https://via.placeholder.com/160x220?text=No+Cover", use_column_width=True)

            with col_right:
                st.subheader(f"[{info['id']}] {info['title']}")
                st.write(f"**作者:** {info['author']}")
                st.write(f"**章节数:** {info['chapter_count']}")
                st.write(f"**总页数:** {info['page_count']}")

                if info["tags"]:
                    st.write("**标签:**")
                    tags_str = ", ".join(info["tags"])
                    st.write(f"{tags_str}")

                st.write("**章节列表:**")
                for i, ch in enumerate(info["chapters"], 1):
                    st.write(f"{i}. {ch['title']} ({ch['pages']}页)")

                if st.button("开始下载 PDF", key="download_btn"):
                    task_id = start_download(info["id"])
                    st.session_state.task_id = task_id
                    st.session_state.download_logs = []
                    st.session_state.download_status = "downloading"

    if "task_id" in st.session_state:
        task_id = st.session_state.task_id
        status = check_download_status(task_id)

        if status.get("status") == "downloading":
            progress = (status["chapter_done"] / status["chapter_total"]) * 100
            st.progress(progress)
            st.write(f"章节 {status['chapter_done']} / {status['chapter_total']}")
            st.write(f"图片 {status['image_done']} / {status['image_total']}")
            if status["text"]:
                st.session_state.download_logs.append(status["text"])
                for log in st.session_state.download_logs[-20:]:
                    st.write(log)
            st.rerun()

        elif status.get("status") == "done":
            st.success("✅ 下载完成！")
            content, filename = get_download_file(task_id)
            if content and filename:
                st.download_button(
                    label="下载文件",
                    data=content,
                    file_name=filename,
                    mime="application/pdf" if filename.endswith(".pdf") else "application/zip"
                )
            st.session_state.pop("task_id")
            st.session_state.pop("download_logs")

        elif "error" in status:
            st.error(f"❌ 下载失败: {status['error']}")
            st.session_state.pop("task_id")
            st.session_state.pop("download_logs")

with tab2:
    col1, col2 = st.columns([4, 1])
    with col1:
        tag = st.text_input("输入标签", placeholder="如：百合、全彩、人妻", key="search_tag")
    with col2:
        st.write("")
        st.write("")
        search_tag_btn = st.button("搜索")

    if search_tag_btn and tag:
        result = search_tag(tag, page=1)
        if "error" in result:
            st.error(f"搜索失败: {result['error']}")
        else:
            st.session_state.search_result = result

    if "search_result" in st.session_state:
        result = st.session_state.search_result
        if result["items"]:
            for item in result["items"]:
                if st.button(f"[{item['id']}] {item['title']}", key=f"search_{item['id']}"):
                    info = get_album_info(item["id"])
                    if "error" not in info:
                        st.session_state.album_info = info
                        st.session_state.album_id = item["id"]
        else:
            st.write("无结果")