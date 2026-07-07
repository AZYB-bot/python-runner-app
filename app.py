import streamlit as st
import os
import re
import shutil
import sys
import tempfile
import random
import zipfile
from io import StringIO

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

        logs = log_buffer.getvalue().strip().split("\n") if log_buffer.getvalue() else []
        return {"status": "done", "content": content, "filename": filename, "logs": logs}

    except Exception as e:
        logs = log_buffer.getvalue().strip().split("\n") if log_buffer.getvalue() else []
        return {"status": "error", "message": str(e), "logs": logs}
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        shutil.rmtree(temp_dir, ignore_errors=True)

st.title("📚 JM Downloader")

tab1, tab2 = st.tabs(["本子下载", "搜索"])

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
            album_id = result["id"]
            st.success(f"🎲 随机百合: [{result['id']}] {result['title']}")
            st.session_state['temp_album_info'] = get_album_info(result["id"])

    if random_guro:
        result = random_album("猎奇")
        if "error" not in result:
            st.session_state['temp_album_id'] = result["id"]
            album_id = result["id"]
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
                st.image(cover_data, width="auto")
            else:
                st.image("https://via.placeholder.com/160x220?text=No+Cover", width="auto")

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