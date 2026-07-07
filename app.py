import streamlit as st
import os
import re
import shutil
import sys
import tempfile
import random
import zipfile
import json
import time
from io import StringIO
from datetime import datetime
from collections import deque

try:
    import jmcomic
    from jmcomic import JmOption, create_option_by_str
    JMCOMIC_AVAILABLE = True
except ImportError:
    JMCOMIC_AVAILABLE = False
    st.error("⚠️ jmcomic 库未安装")

st.set_page_config(page_title="JM Downloader", layout="wide")

if not JMCOMIC_AVAILABLE:
    st.stop()

# ── 配置 ──────────────────────────────────────────────
ADMIN_PASSWORD = "dahan123"
STATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".stats.json")

# ── 持久化统计 ────────────────────────────────────────
def _load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "total_requests": 0,
        "total_downloads": 0,
        "total_bytes": 0,
        "start_time": datetime.now().isoformat(),
        "download_logs": [],
    }

def _save_stats(stats):
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f, ensure_ascii=False)
    except Exception:
        pass

_stats = _load_stats()

def _log_request():
    _stats["total_requests"] += 1
    entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    _stats.setdefault("request_logs", []).insert(0, entry)
    if len(_stats["request_logs"]) > 200:
        _stats["request_logs"] = _stats["request_logs"][:200]
    _save_stats(_stats)

def _log_download(album_id, filename, size, status, ip=""):
    log_entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "album_id": album_id,
        "filename": filename,
        "size": size,
        "status": status,
        "ip": ip,
    }
    _stats["total_downloads"] += 1
    _stats["total_bytes"] += size
    _stats["download_logs"].insert(0, log_entry)
    if len(_stats["download_logs"]) > 200:
        _stats["download_logs"] = _stats["download_logs"][:200]
    _save_stats(_stats)

def fmt_bytes(b):
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b / 1024 / 1024:.1f} MB"

OPTION_BASE = {
    "download": {
        "cache": True,
        "image": {"decode": True, "suffix": ".jpg"},
        "threading": {"image": 50, "photo": 20},
    },
    "client": {"impl": "api", "retry_times": 3, "timeout": 15},
}

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

def get_top_album():
    """获取全站排行榜第一的本子"""
    today = datetime.now().strftime("%Y%m%d")
    cache = st.session_state.get('daily_top')
    if cache and cache.get('date') == today:
        return cache

    try:
        opt = create_option_by_str("log: false\nclient: {impl: api, retry_times: 5}")
        client = opt.build_jm_client()
        # 用热门标签搜索获取排行榜第一页
        tags = ["全彩", "百合", "人妻"]
        items = []
        for tag in tags:
            try:
                result = client.search_tag(f"+{tag}", page=1)
                items = list(result.iter_id_title())
                if items:
                    break
            except Exception:
                continue
        if not items:
            return None
        album_id, title = items[0]
        rec = {"date": today, "id": album_id, "title": title}
        st.session_state['daily_top'] = rec
        return rec
    except Exception:
        return None

def download_album_sync(album_id: str):
    temp_dir = tempfile.mkdtemp(prefix=f"jm_{album_id}_")
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    log_buffer = StringIO()
    sys.stdout = log_buffer
    sys.stderr = log_buffer

    try:
        option = _build_option(temp_dir)
        option.download_album(album_id)

        pdf_files = sorted(
            [f for f in os.listdir(temp_dir) if f.endswith(".pdf")],
            key=lambda x: int(re.sub(r"\D", "", x) or 0),
        )
        if not pdf_files:
            raise RuntimeError("未生成 PDF 文件")

        content = None
        filename = ""
        if len(pdf_files) == 1:
            final_path = os.path.join(temp_dir, f"{album_id}.pdf")
            os.rename(os.path.join(temp_dir, pdf_files[0]), final_path)
            with open(final_path, "rb") as f:
                content = f.read()
            filename = f"{album_id}.pdf"
        else:
            zip_path = os.path.join(temp_dir, f"{album_id}.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for pf in pdf_files:
                    zf.write(os.path.join(temp_dir, pf), pf)
            with open(zip_path, "rb") as f:
                content = f.read()
            filename = f"{album_id}.zip"

        size = len(content) if content else 0
        _log_download(album_id, filename, size, "已完成")

        logs = log_buffer.getvalue().strip().split("\n") if log_buffer.getvalue() else []
        return {"status": "done", "content": content, "filename": filename, "logs": logs}

    except Exception as e:
        _log_download(album_id, "", 0, "失败")
        logs = log_buffer.getvalue().strip().split("\n") if log_buffer.getvalue() else []
        return {"status": "error", "message": str(e), "logs": logs}
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        shutil.rmtree(temp_dir, ignore_errors=True)

# ── 记录请求 ──────────────────────────────────────────
_log_request()

# ── UI ────────────────────────────────────────────────
st.title("📚 JM Downloader")

tab1, tab2, tab3 = st.tabs(["本子下载", "搜索", "管理面板"])

with tab1:
    col1, col2, col3 = st.columns([4, 2, 2])
    
    with col1:
        album_id = st.text_input("输入本子号", placeholder="如 422866")
    
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
            st.session_state['temp_album_id'] = result["id"]
            st.success(f"🎲 随机百合: [{result['id']}] {result['title']}")
            st.session_state['temp_album_info'] = get_album_info(result["id"])

    if random_guro:
        result = random_album("猎奇")
        if "error" not in result:
            st.session_state['temp_album_id'] = result["id"]
            st.success(f"🎲 随机猎奇: [{result['id']}] {result['title']}")
            st.session_state['temp_album_info'] = get_album_info(result["id"])

    if search_btn and album_id:
        info = get_album_info(album_id)
        if "error" in info:
            st.error(f"获取失败: {info['error']}")
        else:
            st.session_state['temp_album_info'] = info

    info = st.session_state.get('temp_album_info')
    if info and "error" not in info:
        col_left, col_right = st.columns([3, 7])
        with col_left:
            cover_data = get_cover_image(info["id"])
            if cover_data:
                st.image(cover_data)
            else:
                st.image("https://via.placeholder.com/160x220?text=No+Cover")

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

            if st.button("开始下载 PDF"):
                progress_bar = st.progress(0)
                status_text = st.empty()
                logs_area = st.empty()
                
                status_text.text(f"正在下载本子 {info['id']}...")
                
                result = download_album_sync(info["id"])
                
                if result["logs"]:
                    logs_area.text("\n".join(result["logs"]))
                
                progress_bar.progress(100)
                
                if result["status"] == "done":
                    status_text.text("✅ 下载完成！")
                    st.download_button(
                        label="下载文件",
                        data=result["content"],
                        file_name=result["filename"],
                        mime="application/pdf" if result["filename"].endswith(".pdf") else "application/zip"
                    )
                else:
                    status_text.text("❌ 下载失败")
                    st.error(result["message"])

with tab2:
    col1, col2 = st.columns([4, 1])
    with col1:
        tag = st.text_input("输入标签", placeholder="如：百合、全彩、人妻")
    with col2:
        st.write("")
        st.write("")
        search_tag_btn = st.button("搜索")

    if search_tag_btn and tag:
        result = search_tag(tag, page=1)
        if "error" in result:
            st.error(f"搜索失败: {result['error']}")
        else:
            st.session_state['search_result'] = result

    search_result = st.session_state.get('search_result')
    if search_result:
        if search_result["items"]:
            for item in search_result["items"]:
                if st.button(f"[{item['id']}] {item['title']}", key=f"search_{item['id']}"):
                    info = get_album_info(item["id"])
                    if "error" not in info:
                        st.session_state['temp_album_info'] = info
        else:
            st.write("无结果")

with tab3:
    # 密码验证
    admin_verified = st.session_state.get('admin_verified', False)
    
    if not admin_verified:
        st.subheader("🔒 管理员登录")
        pwd = st.text_input("请输入管理员密码", type="password")
        if st.button("登录"):
            if pwd == ADMIN_PASSWORD:
                st.session_state['admin_verified'] = True
                st.success("登录成功！")
                st.rerun()
            else:
                st.error("密码错误")
    else:
        # 已登录 - 显示管理面板
        st.subheader("📊 管理面板")
        
        if st.button("退出登录"):
            st.session_state['admin_verified'] = False
            st.rerun()
        
        # 流量概览
        st.markdown("### 流量概览")
        col1, col2, col3, col4 = st.columns(4)
        start = datetime.fromisoformat(_stats["start_time"])
        uptime = int((datetime.now() - start).total_seconds())
        
        with col1:
            st.metric("总请求数", _stats["total_requests"])
        with col2:
            st.metric("总下载数", _stats["total_downloads"])
        with col3:
            st.metric("总流量", fmt_bytes(_stats["total_bytes"]))
        with col4:
            h = uptime // 3600
            m = (uptime % 3600) // 60
            st.metric("运行时间", f"{h}h{m}m")
        
        # 下载任务列表
        st.markdown("### 📥 下载任务")
        logs = _stats.get("download_logs", [])
        if logs:
            table_data = []
            for log in logs[:50]:
                table_data.append([
                    log["time"],
                    log["album_id"],
                    log["filename"],
                    fmt_bytes(log["size"]),
                    log["status"],
                ])
            st.dataframe(
                table_data,
                column_config={
                    "0": "时间",
                    "1": "本子号",
                    "2": "文件名",
                    "3": "大小",
                    "4": "状态",
                },
                use_container_width=True,
                hide_index=True,
                height=400,
            )
        else:
            st.info("暂无下载记录")
        
        # 请求日志
        st.markdown("### 🌐 请求日志")
        st.write(f"自 {start.strftime('%Y-%m-%d %H:%M:%S')} 启动以来，共处理 {_stats['total_requests']} 次请求。")
        
        req_logs = _stats.get("request_logs", [])
        if req_logs:
            req_data = [[i + 1, log["time"]] for i, log in enumerate(req_logs[:50])]
            st.dataframe(
                req_data,
                column_config={
                    "0": "#",
                    "1": "访问时间",
                },
                use_container_width=True,
                hide_index=True,
                height=300,
            )
        else:
            st.info("暂无请求记录")
        
        # 清除统计
        if st.button("🗑️ 清除所有统计"):
            _stats.update({
                "total_requests": 0,
                "total_downloads": 0,
                "total_bytes": 0,
                "start_time": datetime.now().isoformat(),
                "download_logs": [],
                "request_logs": [],
            })
            _save_stats(_stats)
            st.success("统计已清除！")
            st.rerun()

# ── 每日推荐 ──────────────────────────────────────────
st.markdown("---")
st.subheader("🌟 每日推荐")
top = get_top_album()
if top:
    col_a, col_b = st.columns([1, 8])
    with col_a:
        cover = get_cover_image(top["id"])
        if cover:
            st.image(cover, width=120)
    with col_b:
        st.write(f"**本子号:** {top['id']}")
        st.write(f"**标题:** {top['title']}")
        if st.button("查看详情", key="daily_rec_btn"):
            info = get_album_info(top["id"])
            if "error" not in info:
                st.session_state['temp_album_info'] = info
                st.rerun()
else:
    st.info("获取每日推荐失败，请稍后再试")