"""
模组翻译整合工具 - 图形化界面版本
"""

import os
import re
import sys
import json
import shutil
import zipfile
import tempfile
import threading
from collections import OrderedDict
from datetime import datetime
from tkinter import *
from tkinter import ttk, filedialog, messagebox, scrolledtext
from openai import OpenAI

# ========== 默认配置 ==========
DEFAULT_CONFIG = {
    "MODS_DIR": "",
    "TARGET_MOD_NAME": "",
    "EXTRA_CFG_NAMES": ["beomyo", "beomyo_mod_info"],
    "OPENAI_BASE_URL": "https://api.openai.com/v1",
    "OPENAI_API_KEY": "",
    "MODEL": "gpt-3.5-turbo",
    "AUTO_TRANSLATE": True,
    "KEEP_TEMP_FILES": True
}

# ========== 常量 ==========
# In a PyInstaller one-file build, ``__file__`` points into the temporary
# extraction directory.  Keep user settings next to the executable instead.
SCRIPT_DIR = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, 'frozen', False) else __file__
))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
OUTPUT_DIR_MISSING = os.path.join(SCRIPT_DIR, "missing_locale")
OUTPUT_DIR_TRANSLATE = os.path.join(SCRIPT_DIR, "missing_locale_translate")

SECTION_RE = re.compile(r'^\s*\[(?P<section>[^\]]+)\]\s*$')
PROMPT_TEMPLATE = """这是异星工场 mod 语言文件，要求贴合游戏内容，保留格式，给我翻译后的中文完整内容，不要有遗漏，不要保留翻译或编辑时用于追踪信息来源的标记，不要翻译[]内的文字，给我代码段的形式，你需要翻译的是等号右边的部分，注意翻译的时候不要影响占位符例如__1__，注意无视并保留 \n (如果有的话) 因为这是 mod 读取的内容，但不要换行，不要翻译和换行处理，注释也要保留和翻译，不要随意换行！不要随意换行！不要随意换行！不要在开头和结尾输出```lua xxxx```类似的标记，我需要纯文本

以下是需要翻译的内容：

{content}
"""


# ========== 核心功能类 ==========
class ModTranslator:
    def __init__(self, config, log_callback=None, progress_callback=None):
        self.config = config
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.is_running = False
        self.should_stop = False

    def log(self, message, level="INFO"):
        if self.log_callback:
            self.log_callback(f"[{level}] {message}")
        else:
            print(f"[{level}] {message}")

    def set_progress(self, value, text=""):
        if self.progress_callback:
            self.progress_callback(value, text)

    # ========== 工具函数 ==========
    def parse_cfg_text(self, text: str):
        sections = OrderedDict()
        current = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith(';') or line.startswith('#'):
                continue
            m = SECTION_RE.match(line)
            if m:
                current = m.group('section')
                if current not in sections:
                    sections[current] = OrderedDict()
                continue
            if '=' in line and current is not None:
                k, v = line.split('=', 1)
                key = k.strip()
                val = v.strip()
                if key:
                    sections[current][key] = val
        return sections

    def serialize_cfg(self, sections: OrderedDict) -> str:
        lines = []
        for sec, kvs in sections.items():
            lines.append(f"[{sec}]")
            for k, v in kvs.items():
                lines.append(f"{k}={v}")
            lines.append("")
        return "\n".join(lines)

    def load_text_from_zip(self, zf, member, encoding='utf-8'):
        with zf.open(member, 'r') as f:
            data = f.read()
        if data.startswith(b'\xef\xbb\xbf'):
            data = data[3:]
        return data.decode(encoding, errors='replace')

    def parse_version_from_name(self, name: str):
        if name.lower().endswith('.zip'):
            name = name[:-4]
        match = re.search(r'[_-](\d+(?:\.\d+)*)$', name)
        if match:
            version_str = match.group(1)
            try:
                version_tuple = tuple(map(int, version_str.split('.')))
                mod_name = name[:match.start()]
                return mod_name, version_tuple, version_str
            except ValueError:
                return name, None, None
        return name, None, None

    def find_latest_target_mod(self, mods_dir: str, target_name: str):
        candidates_folder = []
        candidates_zip = []

        if not os.path.exists(mods_dir):
            return None

        for item in os.listdir(mods_dir):
            item_path = os.path.join(mods_dir, item)
            if os.path.isdir(item_path):
                mod_name, version_tuple, _ = self.parse_version_from_name(item)
                if mod_name == target_name and version_tuple is not None:
                    candidates_folder.append((version_tuple, item_path, item))

        for filename in os.listdir(mods_dir):
            if not filename.lower().endswith('.zip'):
                continue
            file_path = os.path.join(mods_dir, filename)
            if os.path.isfile(file_path):
                mod_name, version_tuple, _ = self.parse_version_from_name(filename)
                if mod_name == target_name and version_tuple is not None:
                    candidates_zip.append((version_tuple, file_path, filename))

        if candidates_folder:
            candidates_folder.sort(reverse=True, key=lambda x: x[0])
            return ('folder',) + candidates_folder[0]

        if candidates_zip:
            candidates_zip.sort(reverse=True, key=lambda x: x[0])
            return ('zip',) + candidates_zip[0]

        return None

    def is_mod_folder(self, path: str) -> bool:
        if not os.path.isdir(path):
            return False
        info_json = os.path.join(path, 'info.json')
        return os.path.exists(info_json) and os.path.isfile(info_json)

    def is_mod_zip(self, path: str) -> bool:
        if not os.path.isfile(path) or not path.lower().endswith('.zip'):
            return False
        try:
            with zipfile.ZipFile(path, 'r') as zf:
                return any('info.json' in name.lower() for name in zf.namelist())
        except:
            return False

    def list_mods(self, mods_dir: str):
        mods = []
        if not os.path.exists(mods_dir):
            return mods
        for item in os.listdir(mods_dir):
            item_path = os.path.join(mods_dir, item)
            if os.path.isdir(item_path) and self.is_mod_folder(item_path):
                mod_name, _, _ = self.parse_version_from_name(item)
                mods.append(('folder', item_path, mod_name))
            elif os.path.isfile(item_path) and self.is_mod_zip(item_path):
                mod_name, _, _ = self.parse_version_from_name(item)
                mods.append(('zip', item_path, mod_name))
        return mods

    def get_mod_root_folder(self, mod_path: str, mod_type: str):
        if mod_type == 'folder':
            return os.path.basename(mod_path)
        else:
            try:
                with zipfile.ZipFile(mod_path, 'r') as zf:
                    names = [name.replace('\\', '/') for name in zf.namelist()]
                    # Prefer the directory containing info.json. A ZIP may
                    # start with a top-level file, so using the first member
                    # as the root can point at the wrong path.
                    for name in names:
                        parts = [part for part in name.split('/') if part]
                        if (len(parts) >= 2 and
                                parts[-1].casefold() == 'info.json'):
                            return parts[0]
                    for name in names:
                        parts = [part for part in name.split('/') if part]
                        if len(parts) >= 2:
                            return parts[0]
            except:
                pass
        return None

    def extract_zip_safely(self, zf, destination: str):
        """Extract regular ZIP members without allowing paths outside destination."""
        destination = os.path.abspath(destination)
        os.makedirs(destination, exist_ok=True)

        for member in zf.infolist():
            name = member.filename.replace('\\', '/')
            parts = [part for part in name.split('/') if part not in ('', '.')]
            if not parts or any(part == '..' for part in parts) or os.path.isabs(name):
                raise ValueError(f"ZIP 中包含非法路径: {member.filename}")
            if re.match(r'^[A-Za-z]:', name):
                raise ValueError(f"ZIP 中包含绝对路径: {member.filename}")

            member_path = os.path.abspath(os.path.join(destination, *parts))
            if os.path.commonpath([destination, member_path]) != destination:
                raise ValueError(f"ZIP 路径超出临时目录: {member.filename}")

            if member.is_dir() or name.endswith('/'):
                os.makedirs(member_path, exist_ok=True)
                continue

            os.makedirs(os.path.dirname(member_path), exist_ok=True)
            with zf.open(member, 'r') as source, open(member_path, 'wb') as target:
                shutil.copyfileobj(source, target)

    def get_mod_languages(self, mod_path: str, mod_type: str):
        en_cfgs = []
        zh_cfgs = []

        if mod_type == 'zip':
            try:
                with zipfile.ZipFile(mod_path, 'r') as zf:
                    for member in zf.namelist():
                        low = member.lower()
                        if not low.endswith('.cfg'):
                            continue
                        try:
                            text = self.load_text_from_zip(zf, member)
                            if '/locale/en/' in low:
                                en_cfgs.append((member, text))
                            elif '/locale/zh-cn/' in low:
                                zh_cfgs.append((member, text))
                        except Exception:
                            pass
            except zipfile.BadZipFile:
                pass
        else:
            for root, _, files in os.walk(mod_path):
                for file in files:
                    if not file.lower().endswith('.cfg'):
                        continue
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            text = f.read()
                        rel_path = os.path.relpath(file_path, mod_path)
                        if 'locale' + os.sep + 'en' in rel_path:
                            en_cfgs.append((rel_path, text))
                        elif 'locale' + os.sep + 'zh-CN' in rel_path:
                            zh_cfgs.append((rel_path, text))
                    except Exception:
                        pass

        return en_cfgs, zh_cfgs

    def get_all_zh_from_target(self, target_path: str, target_type: str, root_folder: str):
        zh_all = {}
        if target_type == 'zip':
            try:
                with zipfile.ZipFile(target_path, 'r') as zf:
                    for member in zf.namelist():
                        lower_member = member.lower()
                        if (f"{root_folder.lower()}/" in lower_member and
                            "locale/zh-cn/" in lower_member and
                            lower_member.endswith('.cfg')):
                            filename = os.path.basename(member)
                            mod_name = os.path.splitext(filename)[0]
                            try:
                                text = self.load_text_from_zip(zf, member)
                                zh_all[mod_name] = self.parse_cfg_text(text)
                            except Exception:
                                pass
            except Exception as e:
                self.log(f"读取目标模组失败: {e}", "ERROR")
        else:
            target_dir = os.path.join(target_path, 'locale', 'zh-CN')
            if os.path.exists(target_dir):
                for filename in os.listdir(target_dir):
                    if filename.lower().endswith('.cfg'):
                        file_path = os.path.join(target_dir, filename)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                text = f.read()
                            mod_name = os.path.splitext(filename)[0]
                            zh_all[mod_name] = self.parse_cfg_text(text)
                        except Exception:
                            pass
        return zh_all

    # ========== 主要步骤 ==========
    def step_separate(self):
        self.log("\n===== 第一步：分离缺失键 =====")
        self.set_progress(0, "正在分离缺失键...")

        if os.path.exists(OUTPUT_DIR_MISSING):
            shutil.rmtree(OUTPUT_DIR_MISSING)
            self.log(f"已删除旧文件夹: {OUTPUT_DIR_MISSING}")
        os.makedirs(OUTPUT_DIR_MISSING, exist_ok=True)

        mods_dir = self.config.get("MODS_DIR", "")
        target_name = self.config.get("TARGET_MOD_NAME", "")
        extra_names = self.config.get("EXTRA_CFG_NAMES", [])

        if not mods_dir or not os.path.exists(mods_dir):
            self.log(f"Mods目录不存在: {mods_dir}", "ERROR")
            return False

        if not target_name:
            self.log("未指定目标模组名称", "ERROR")
            return False

        target_info = self.find_latest_target_mod(mods_dir, target_name)
        if not target_info:
            self.log(f"未找到 {target_name}_版本 的文件夹或zip文件", "ERROR")
            return False

        target_type, version_tuple, target_path, target_name_found = target_info
        self.log(f"目标模组: {target_name_found} (类型: {target_type})")

        root_folder = self.get_mod_root_folder(target_path, target_type)
        if root_folder is None:
            self.log("无法确定目标模组根目录", "ERROR")
            return False

        zh_all = self.get_all_zh_from_target(target_path, target_type, root_folder)

        extra_cfgs = {}
        for special in extra_names:
            if special in zh_all:
                extra_cfgs[special] = zh_all[special]

        mods = self.list_mods(mods_dir)
        mods.sort(key=lambda x: x[2])
        total_mods = 0
        total_keys = 0

        for idx, (mod_type, mod_path, mod_name) in enumerate(mods):
            if self.should_stop:
                self.log("用户中断操作", "WARN")
                return False

            if mod_name == target_name:
                continue

            en_cfgs, _ = self.get_mod_languages(mod_path, mod_type)
            if not en_cfgs:
                continue

            en_sections_merged = OrderedDict()
            for _, text in en_cfgs:
                parsed = self.parse_cfg_text(text)
                for sec, kvs in parsed.items():
                    if sec not in en_sections_merged:
                        en_sections_merged[sec] = OrderedDict()
                    en_sections_merged[sec].update(kvs)

            _, mod_zh_cfgs = self.get_mod_languages(mod_path, mod_type)
            mod_zh_sections = OrderedDict()
            for _, text in mod_zh_cfgs:
                parsed = self.parse_cfg_text(text)
                for sec, kvs in parsed.items():
                    if sec not in mod_zh_sections:
                        mod_zh_sections[sec] = OrderedDict()
                    mod_zh_sections[sec].update(kvs)

            external_zh_sections = zh_all.get(mod_name, OrderedDict())

            missing = OrderedDict()
            for sec, kvs in en_sections_merged.items():
                for k, v in kvs.items():
                    found = False
                    if sec in mod_zh_sections and k in mod_zh_sections[sec]:
                        found = True
                    elif sec in external_zh_sections and k in external_zh_sections[sec]:
                        found = True
                    else:
                        for bm in extra_cfgs.values():
                            if sec in bm and k in bm[sec]:
                                found = True
                                break
                    if not found:
                        if sec not in missing:
                            missing[sec] = OrderedDict()
                        if k not in missing[sec]:
                            missing[sec][k] = v

            if missing:
                cfg_file = os.path.join(OUTPUT_DIR_MISSING, f"{mod_name}.cfg")
                with open(cfg_file, 'w', encoding='utf-8', newline='\n') as fw:
                    for sec, kvs in missing.items():
                        fw.write(f"[{sec}]\n")
                        for k, v in kvs.items():
                            fw.write(f"{k}={v}\n")
                        fw.write("\n")
                key_count = sum(len(kvs) for kvs in missing.values())
                total_mods += 1
                total_keys += key_count
                self.log(f"{mod_name}.cfg ({key_count} keys)")

            self.set_progress(10 + 40 * (idx + 1) // len(mods), f"处理中: {mod_name}")

        self.log(f"第一步完成：共 {total_mods} 个mod，{total_keys} 个缺失键")
        self.set_progress(50, "第一步完成")
        return True

    def step_translate(self):
        self.log("\n===== 第二步：AI翻译 =====")
        self.set_progress(50, "正在翻译...")

        if not os.path.exists(OUTPUT_DIR_MISSING):
            self.log(f"输入目录不存在: {OUTPUT_DIR_MISSING}", "ERROR")
            return False

        if os.path.exists(OUTPUT_DIR_TRANSLATE):
            shutil.rmtree(OUTPUT_DIR_TRANSLATE)
            self.log(f"已删除旧文件夹: {OUTPUT_DIR_TRANSLATE}")
        os.makedirs(OUTPUT_DIR_TRANSLATE, exist_ok=True)

        cfg_files = [f for f in os.listdir(OUTPUT_DIR_MISSING) if f.endswith('.cfg')]
        cfg_files.sort()
        if not cfg_files:
            self.log("没有找到任何cfg文件", "WARN")
            return True

        self.log(f"找到 {len(cfg_files)} 个cfg文件")

        try:
            client = OpenAI(
                base_url=self.config.get("OPENAI_BASE_URL", ""),
                api_key=self.config.get("OPENAI_API_KEY", ""),
            )
        except Exception as e:
            self.log(f"初始化OpenAI客户端失败: {e}", "ERROR")
            return False

        success_count = 0
        fail_count = 0

        for idx, filename in enumerate(cfg_files, 1):
            if self.should_stop:
                self.log("用户中断操作", "WARN")
                return False

            input_path = os.path.join(OUTPUT_DIR_MISSING, filename)
            output_path = os.path.join(OUTPUT_DIR_TRANSLATE, filename)
            self.log(f"[{idx}/{len(cfg_files)}] 正在处理: {filename}")

            with open(input_path, 'r', encoding='utf-8') as f:
                content = f.read()

            prompt = PROMPT_TEMPLATE.format(content=content)
            try:
                response = client.chat.completions.create(
                    model=self.config.get("MODEL", "gpt-3.5-turbo"),
                    messages=[{"role": "user", "content": prompt}],
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                translated = response.choices[0].message.content
                if translated is None:
                    raise Exception("翻译返回空")
                final_content = self.extract_code_block(translated)
                with open(output_path, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(final_content)
                success_count += 1
                self.log(f"  [OK] 翻译完成 -> {filename}")
            except Exception as e:
                fail_count += 1
                self.log(f"  [ERROR] 翻译失败: {e}", "ERROR")

            self.set_progress(50 + 40 * idx // len(cfg_files), f"翻译中: {filename}")

        self.log(f"第二步完成：成功 {success_count}, 失败 {fail_count}")
        self.set_progress(90, "翻译完成")
        return success_count > 0

    def extract_code_block(self, text: str) -> str:
        pattern = re.compile(r'```(?:cfg|ini|text)?\s*\n(.*?)\n```', re.DOTALL)
        matches = pattern.findall(text)
        if matches:
            content = matches[-1].strip()
        else:
            content = text.strip()
        content = self.clean_code_blocks(content)
        return content

    def clean_code_blocks(self, text: str) -> str:
        lines = text.splitlines()
        if not lines:
            return text
        if lines and lines[0].strip().startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith('```'):
            lines = lines[:-1]
        if lines and lines[0].strip().endswith('```'):
            lines[0] = lines[0].strip()[:-3].rstrip()
            if not lines[0]:
                lines = lines[1:]
        if lines and lines[-1].strip().startswith('```'):
            lines[-1] = lines[-1].strip()[3:].lstrip()
            if not lines[-1]:
                lines = lines[:-1]
        return '\n'.join(lines)

    def step_append(self):
        self.log("\n===== 第三步：合并并替换原模组 =====")
        self.set_progress(90, "正在合并...")

        if not os.path.exists(OUTPUT_DIR_TRANSLATE):
            self.log(f"翻译目录不存在: {OUTPUT_DIR_TRANSLATE}", "ERROR")
            return False

        translate_files = [f for f in os.listdir(OUTPUT_DIR_TRANSLATE) if f.endswith('.cfg')]
        if not translate_files:
            self.log("没有找到任何翻译文件", "WARN")
            return True
        translate_files.sort()
        self.log(f"找到 {len(translate_files)} 个翻译文件")

        mods_dir = self.config.get("MODS_DIR", "")
        target_name = self.config.get("TARGET_MOD_NAME", "")

        target_info = self.find_latest_target_mod(mods_dir, target_name)
        if not target_info:
            self.log(f"未找到 {target_name}_版本 的文件夹或zip文件", "ERROR")
            return False

        target_type, version_tuple, target_path, target_name_found = target_info
        self.log(f"目标模组: {target_name_found} (类型: {target_type})")

        if target_type == 'folder':
            zh_cn_dir = os.path.join(target_path, 'locale', 'zh-CN')
            os.makedirs(zh_cn_dir, exist_ok=True)
            self.log(f"zh-CN 目录: {zh_cn_dir}")

            success_count = 0
            for idx, filename in enumerate(translate_files, 1):
                if self.should_stop:
                    self.log("用户中断操作", "WARN")
                    return False

                target_file_path = os.path.join(zh_cn_dir, filename)
                translate_path = os.path.join(OUTPUT_DIR_TRANSLATE, filename)
                self.log(f"[{idx}/{len(translate_files)}] 处理: {filename}")

                try:
                    with open(translate_path, 'r', encoding='utf-8') as f:
                        new_content = f.read()
                    if os.path.exists(target_file_path):
                        with open(target_file_path, 'r', encoding='utf-8') as f:
                            existing_content = f.read()
                        new_parsed = self.parse_cfg_text(new_content)
                        merged_parsed = self.parse_cfg_text(existing_content)
                        for sec, kvs in new_parsed.items():
                            if sec not in merged_parsed:
                                merged_parsed[sec] = OrderedDict()
                            merged_parsed[sec].update(kvs)
                        final_content = self.serialize_cfg(merged_parsed)
                    else:
                        final_content = new_content
                    with open(target_file_path, 'w', encoding='utf-8', newline='\n') as f:
                        f.write(final_content)
                    success_count += 1
                    self.log(f"  [OK] 已{'更新' if os.path.exists(target_file_path) else '创建'}: {filename}")
                except Exception as e:
                    self.log(f"  [ERROR] 处理失败: {e}", "ERROR")

            self.log(f"合并完成，成功 {success_count}/{len(translate_files)}")
            if success_count > 0:
                self.log(f"已更新模组文件夹: {target_path}")

        else:  # zip
            staging_path = None
            try:
                with tempfile.TemporaryDirectory() as temp_dir:
                    self.log("正在解压模组...")
                    with zipfile.ZipFile(target_path, 'r') as zf:
                        bad_member = zf.testzip()
                        if bad_member:
                            self.log(f"原模组 ZIP 已损坏，无法读取: {bad_member}", "ERROR")
                            return False
                        self.extract_zip_safely(zf, temp_dir)

                    root_folder = self.get_mod_root_folder(target_path, target_type)
                    if not root_folder:
                        self.log("无法确定模组根目录", "ERROR")
                        return False
                    root_path = os.path.join(temp_dir, root_folder)
                    if not os.path.isdir(root_path):
                        self.log(f"解压后找不到模组根目录: {root_folder}", "ERROR")
                        return False

                    zh_cn_dir = None
                    for root, dirs, files in os.walk(root_path):
                        if (os.path.basename(root).casefold() == 'zh-cn' and
                                os.path.basename(os.path.dirname(root)).casefold() == 'locale'):
                            zh_cn_dir = root
                            break
                    if not zh_cn_dir:
                        zh_cn_dir = os.path.join(root_path, 'locale', 'zh-CN')
                        os.makedirs(zh_cn_dir, exist_ok=True)
                        self.log(f"创建 zh-CN 目录: {zh_cn_dir}")

                    self.log(f"zh-CN 目录: {zh_cn_dir}")

                    success_count = 0
                    for idx, filename in enumerate(translate_files, 1):
                        if self.should_stop:
                            self.log("用户中断操作", "WARN")
                            return False

                        translate_path = os.path.join(OUTPUT_DIR_TRANSLATE, filename)
                        target_file_path = os.path.join(zh_cn_dir, filename)
                        self.log(f"[{idx}/{len(translate_files)}] 处理: {filename}")

                        try:
                            with open(translate_path, 'r', encoding='utf-8') as f:
                                new_content = f.read()
                            if os.path.exists(target_file_path):
                                with open(target_file_path, 'r', encoding='utf-8') as f:
                                    existing_content = f.read()
                                new_parsed = self.parse_cfg_text(new_content)
                                merged_parsed = self.parse_cfg_text(existing_content)
                                for sec, kvs in new_parsed.items():
                                    if sec not in merged_parsed:
                                        merged_parsed[sec] = OrderedDict()
                                    merged_parsed[sec].update(kvs)
                                final_content = self.serialize_cfg(merged_parsed)
                            else:
                                final_content = new_content
                            with open(target_file_path, 'w', encoding='utf-8', newline='\n') as f:
                                f.write(final_content)
                            success_count += 1
                            self.log(f"  [OK] 已处理: {filename}")
                        except Exception as e:
                            self.log(f"  [ERROR] 处理失败: {e}", "ERROR")

                    self.log(f"合并完成，成功 {success_count}/{len(translate_files)}")

                    if success_count == 0:
                        self.log("没有成功处理任何文件，不生成新模组", "WARN")
                        return False

                    # Build and validate beside the original. The original ZIP
                    # remains untouched until the complete replacement is ready.
                    self.log("正在打包新模组...")
                    target_dir = os.path.dirname(target_path) or '.'
                    staging_fd, staging_path = tempfile.mkstemp(
                        prefix=f".{os.path.basename(target_path)}.",
                        suffix='.tmp',
                        dir=target_dir
                    )
                    os.close(staging_fd)

                    with zipfile.ZipFile(staging_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                        # Walk the whole extracted tree. Packing only root_folder
                        # silently dropped files stored beside it in some mods.
                        for root, dirs, files in os.walk(temp_dir):
                            for directory in dirs:
                                directory_path = os.path.join(root, directory)
                                arcname = os.path.relpath(
                                    directory_path, temp_dir
                                ).replace(os.sep, '/') + '/'
                                # Keep explicit empty-directory entries such as
                                # panglia_planet/sounds/.
                                zf.writestr(arcname, b'')
                            for file in files:
                                file_path = os.path.join(root, file)
                                arcname = os.path.relpath(
                                    file_path, temp_dir
                                ).replace(os.sep, '/')
                                zf.write(file_path, arcname)

                    with zipfile.ZipFile(staging_path, 'r') as zf:
                        bad_member = zf.testzip()
                        if bad_member:
                            raise zipfile.BadZipFile(
                                f"生成的 ZIP 校验失败: {bad_member}"
                            )

                    os.replace(staging_path, target_path)
                    staging_path = None
                    self.log(f"新模组已生成并原子替换原文件: {target_path}")
            except Exception as e:
                if staging_path and os.path.exists(staging_path):
                    try:
                        os.remove(staging_path)
                    except OSError as cleanup_error:
                        self.log(f"清理临时 ZIP 失败: {cleanup_error}", "WARN")
                self.log(f"替换 ZIP 失败，原模组未修改: {e}", "ERROR")
                return False

        self.set_progress(100, "全部完成")
        return True

    def run(self):
        """运行完整流程"""
        self.is_running = True
        self.should_stop = False

        try:
            if not self.step_separate():
                return False
            if not self.config.get("AUTO_TRANSLATE", True):
                self.log("已跳过翻译步骤（AUTO_TRANSLATE=False）")
                return True
            if not self.step_translate():
                return False
            if not self.step_append():
                return False
            return True
        except Exception as e:
            self.log(f"运行出错: {e}", "ERROR")
            import traceback
            self.log(traceback.format_exc(), "ERROR")
            return False
        finally:
            self.is_running = False


# ========== 图形界面 ==========
class ModTranslatorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("模组翻译整合工具")
        self.root.geometry("760x700")
        self.root.resizable(True, True)

        # 加载配置
        self.config = self.load_config()

        # 状态
        self.translator = None
        self.is_running = False
        self.worker_thread = None

        # 创建界面
        self.create_widgets()

        # 绑定关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def load_config(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in config:
                        config[k] = v
                return config
            except Exception:
                return DEFAULT_CONFIG.copy()
        return DEFAULT_CONFIG.copy()

    def save_config(self):
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            messagebox.showerror("保存失败", f"无法保存配置: {e}")

    def create_widgets(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=BOTH, expand=True)

        config_frame = ttk.LabelFrame(main_frame, text="翻译设置", padding="10")
        config_frame.pack(fill=X, pady=(0, 10))

        ttk.Label(config_frame, text="Mods目录:").grid(row=0, column=0, sticky=W, pady=2)
        self.mods_dir_var = StringVar(value=self.config.get("MODS_DIR", ""))
        mods_dir_entry = ttk.Entry(config_frame, textvariable=self.mods_dir_var, width=50)
        mods_dir_entry.grid(row=0, column=1, sticky=EW, pady=2, padx=(5, 5))
        ttk.Button(config_frame, text="浏览", command=self.browse_mods_dir).grid(row=0, column=2, pady=2)

        ttk.Label(config_frame, text="目标模组:").grid(row=1, column=0, sticky=W, pady=2)
        self.target_name_var = StringVar(value=self.config.get("TARGET_MOD_NAME", ""))
        self.target_mod_combo = ttk.Combobox(config_frame, textvariable=self.target_name_var, state="readonly")
        self.target_mod_combo.grid(row=1, column=1, sticky=EW, pady=2, padx=(5, 5))
        ttk.Button(config_frame, text="刷新", command=self.refresh_target_mods).grid(row=1, column=2, pady=2)

        # ===== API配置 =====
        api_frame = ttk.LabelFrame(main_frame, text="API 配置", padding="10")
        api_frame.pack(fill=X, pady=(0, 10))

        ttk.Label(api_frame, text="API Base URL:").grid(row=0, column=0, sticky=W, pady=2)
        self.api_url_var = StringVar(value=self.config.get("OPENAI_BASE_URL", ""))
        ttk.Entry(api_frame, textvariable=self.api_url_var, width=50).grid(row=0, column=1, sticky=EW, pady=2, padx=(5, 5))

        ttk.Label(api_frame, text="API Key:").grid(row=1, column=0, sticky=W, pady=2)
        self.api_key_var = StringVar(value=self.config.get("OPENAI_API_KEY", ""))
        api_key_entry = ttk.Entry(api_frame, textvariable=self.api_key_var, width=50, show="*")
        api_key_entry.grid(row=1, column=1, sticky=EW, pady=2, padx=(5, 5))
        ttk.Button(api_frame, text="显示/隐藏", command=self.toggle_api_key_visibility).grid(row=1, column=2, pady=2)
        self.api_key_entry = api_key_entry

        ttk.Label(api_frame, text="模型:").grid(row=2, column=0, sticky=W, pady=2)
        self.model_var = StringVar(value=self.config.get("MODEL", "gpt-3.5-turbo"))
        ttk.Entry(api_frame, textvariable=self.model_var, width=50).grid(row=2, column=1, sticky=EW, pady=2, padx=(5, 5))

        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=X, pady=(0, 10))

        self.start_btn = ttk.Button(btn_frame, text="执行翻译", command=self.start_process)
        self.start_btn.pack(side=LEFT, padx=(0, 10))

        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self.stop_process, state=DISABLED)
        self.stop_btn.pack(side=LEFT, padx=(0, 10))

        ttk.Button(btn_frame, text="保存配置", command=self.save_current_config).pack(side=LEFT, padx=(0, 10))

        # 进度条
        self.progress_var = DoubleVar()
        self.progress_bar = ttk.Progressbar(main_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=X, pady=(0, 10))

        self.progress_text = StringVar(value="就绪")
        ttk.Label(main_frame, textvariable=self.progress_text).pack(anchor=W, pady=(0, 5))

        # 日志区域
        log_frame = ttk.LabelFrame(main_frame, text="日志", padding="5")
        log_frame.pack(fill=BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=WORD, height=15, font=("Consolas", 9))
        self.log_text.pack(fill=BOTH, expand=True)

        config_frame.columnconfigure(1, weight=1)
        api_frame.columnconfigure(1, weight=1)

        self.apply_config_to_ui()
        self.refresh_target_mods()

    def apply_config_to_ui(self):
        self.mods_dir_var.set(self.config.get("MODS_DIR", ""))
        self.target_name_var.set(self.config.get("TARGET_MOD_NAME", ""))
        self.api_url_var.set(self.config.get("OPENAI_BASE_URL", ""))
        self.api_key_var.set(self.config.get("OPENAI_API_KEY", ""))
        self.model_var.set(self.config.get("MODEL", "gpt-3.5-turbo"))

    def save_current_config(self):
        self.update_config_from_ui()
        self.save_config()
        messagebox.showinfo("保存成功", "配置已保存")

    def update_config_from_ui(self):
        self.config["MODS_DIR"] = self.mods_dir_var.get().strip()
        self.config["TARGET_MOD_NAME"] = self.target_name_var.get().strip()
        self.config["OPENAI_BASE_URL"] = self.api_url_var.get().strip()
        self.config["OPENAI_API_KEY"] = self.api_key_var.get().strip()
        self.config["MODEL"] = self.model_var.get().strip()

    def browse_mods_dir(self):
        path = filedialog.askdirectory(title="选择 Factorio mods 目录")
        if path:
            self.mods_dir_var.set(path)
            self.refresh_target_mods()

    def refresh_target_mods(self):
        """Scan the selected directory and offer each installed mod once."""
        mods_dir = self.mods_dir_var.get().strip()
        if not os.path.isdir(mods_dir):
            self.target_mod_combo["values"] = ()
            return

        scanner = ModTranslator(self.config)
        names = sorted({mod_name for _, _, mod_name in scanner.list_mods(mods_dir)}, key=str.casefold)
        self.target_mod_combo["values"] = names
        current = self.target_name_var.get().strip()
        if current not in names:
            self.target_name_var.set("")

    def toggle_api_key_visibility(self):
        current = self.api_key_entry.cget("show")
        self.api_key_entry.config(show="" if current == "*" else "*")

    def log_callback(self, message):
        def _log():
            self.log_text.insert(END, message + "\n")
            self.log_text.see(END)
        self.root.after(0, _log)

    def progress_callback(self, value, text=""):
        def _update():
            self.progress_var.set(value)
            if text:
                self.progress_text.set(text)
        self.root.after(0, _update)

    def start_process(self):
        if self.is_running:
            return

        # 更新配置
        self.update_config_from_ui()
        self.save_config()

        # 验证配置
        if not self.config.get("MODS_DIR"):
            messagebox.showerror("错误", "请指定Mods目录")
            return
        if not os.path.exists(self.config.get("MODS_DIR", "")):
            messagebox.showerror("错误", f"Mods目录不存在: {self.config.get('MODS_DIR')}")
            return
        if not self.config.get("TARGET_MOD_NAME"):
            messagebox.showerror("错误", "请指定目标模组名称")
            return
        if self.config.get("AUTO_TRANSLATE", True):
            if not self.config.get("OPENAI_API_KEY"):
                messagebox.showerror("错误", "请填写API Key")
                return
            if not self.config.get("OPENAI_BASE_URL"):
                messagebox.showerror("错误", "请填写API Base URL")
                return

        self.is_running = True
        self.start_btn.config(state=DISABLED)
        self.stop_btn.config(state=NORMAL)
        self.progress_var.set(0)
        self.progress_text.set("开始处理...")
        self.log_text.delete(1.0, END)

        # 在后台线程运行
        self.translator = ModTranslator(
            self.config,
            log_callback=self.log_callback,
            progress_callback=self.progress_callback
        )

        def run_worker():
            try:
                success = self.translator.run()
                self.root.after(0, lambda: self.on_process_finished(success))
            except Exception as e:
                self.root.after(0, lambda: self.on_process_finished(False, str(e)))

        self.worker_thread = threading.Thread(target=run_worker, daemon=True)
        self.worker_thread.start()

    def stop_process(self):
        if self.translator:
            self.translator.should_stop = True
            self.log_callback("正在停止... 请稍候")
        self.stop_btn.config(state=DISABLED)

    def on_process_finished(self, success, error=None):
        self.is_running = False
        self.start_btn.config(state=NORMAL)
        self.stop_btn.config(state=DISABLED)

        if success:
            self.progress_text.set("处理完成!")
            self.log_callback("\n===== 全部完成 =====")
            if self.config.get("KEEP_TEMP_FILES", True):
                self.log_callback("临时文件目录: missing_locale 和 missing_locale_translate 已保留，可手动删除。")
        else:
            if error:
                self.progress_text.set(f"处理失败: {error}")
            else:
                self.progress_text.set("处理被中断或失败")
            messagebox.showerror("处理失败", f"处理过程中出现错误，请查看日志")

    def on_close(self):
        if self.is_running:
            if messagebox.askyesno("确认退出", "处理正在进行中，确定要退出吗？"):
                self.stop_process()
                self.root.destroy()
        else:
            self.root.destroy()

    def clean_temp_files(self):
        if messagebox.askyesno("确认清理", "确定要删除所有临时文件吗？\n(包括 missing_locale 和 missing_locale_translate 目录)"):
            for dir_path in [OUTPUT_DIR_MISSING, OUTPUT_DIR_TRANSLATE]:
                if os.path.exists(dir_path):
                    try:
                        shutil.rmtree(dir_path)
                        self.log_callback(f"已删除: {dir_path}")
                    except Exception as e:
                        self.log_callback(f"[ERROR] 删除失败: {e}")
            messagebox.showinfo("清理完成", "临时文件已清理")


# ========== 启动 ==========
def main():
    root = Tk()
    app = ModTranslatorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
