# Mod 全自动翻译脚本

可以找点免费api，比如 https://bailian.console.aliyun.com/cn-beijing?tab=costing-balance#/costing-balance/free-quota

## 配置文件说明，（不必修改配置文件，已提供交互界面）

```json
{    //你的mod目录
    "MODS_DIR": "D:\\Program Files\\Users\\Win10\\Administrator\\AppData\\Roaming\\Factorio\\mods",
    
    //你要用哪个mod当翻译mod？翻译好的内容会塞到这里
    "TARGET_MOD_NAME": "zzzz-beomyo-fix",
    
    //上述mod\locale\zh-CN里有没有你自己翻译的cfg文件，如果没有留空即可
    "EXTRA_CFG_NAMES": [
        "beomyo",
        "beomyo_mod_info"
    ],
    
    //你的大模型请求地址
    "OPENAI_BASE_URL": "https://aliyuncs.com/v1",
    
    //你的api-key
    "OPENAI_API_KEY": "sk-123456qwert",
    
    //你的模型
    "MODEL": "deepseek-v4-flash",
    
    //有一些专有名称音译很难受，手动替换一下（可能不准确，毕竟是音译），替换效果一般
    "TRANSLATION_MAP": [
        ["阿巴卡伊巴","骸殖星"], 
        ["努瓦斯","新地星"]
    ],
    
    //是否调用大模型，false表示仅仅输出缺失的翻译文件
    "AUTO_TRANSLATE": true,
    
    //是否保留生成的中间文件
    "KEEP_TEMP_FILES": true
}
```

***有bug 反馈即可***

## 脚本逻辑详解

---

### 第一步：`step_separate()` —— 扫描并提取缺失翻译

这一步的目标是 **找出所有模组中还没有中文翻译的文本**。

#### 执行流程

1. **定位目标模组**  
   调用 `find_latest_target_mod()`，在 `MODS_DIR` 中搜索 `TARGET_MOD_NAME`（即 `zzzz-beomyo-fix`）。  

   - 优先找文件夹（如 `zzzz-beomyo-fix_1.0.0/`）  
   - 找不到则找 zip 包（如 `zzzz-beomyo-fix_1.0.0.zip`）  
   - 按版本号排序，取最新版

2. **加载目标模组的所有中文翻译作为“参考库”**  
   读取目标模组 `locale/zh-CN/` 下所有 `.cfg` 文件，解析成 `{ 段落名: { 键: 值 } }` 的结构，存为 `zh_all`。

3. **提取额外参考翻译**  
   从 `zh_all` 中取出 `EXTRA_CFG_NAMES` 指定的配置文件（如 `beomyo`、`beomyo_mod_info`），作为补充参考库 `extra_cfgs`。

4. **遍历所有其他模组**  

   - 对每个模组，读取其 `locale/en/` 下的所有英文 `.cfg` 文件，合并成一张完整的“英文键值表”。
   - 再读取该模组自带的 `locale/zh-CN/`，得到“自带中文表”。
   - 从 `zh_all` 中取出该模组对应的翻译（如果目标模组里已有），作为“外部中文表”。

5. **逐条比对，找出缺失项**  
   对英文表中的每一个 `(段落, 键)`：

   - 先查“自带中文表”有没有 → 有则跳过
   - 再查“外部中文表”有没有 → 有则跳过
   - 再查 `extra_cfgs` 中所有配置里有没有 → 有则跳过
   - **都没有** → 记录到 `missing` 中，键对应的值为**英文原文**

6. **输出缺失文件**  
   每个模组生成一个独立的 `.cfg` 文件，存放在 `missing_locale/` 目录下，文件名即模组名。  
   文件内容格式为标准 cfg 格式：

   ```cfg
   [段落名]
   键1=英文原文1
   键2=英文原文2
   
   [另一段落]
   键3=英文原文3
   ```

> 所有翻译来源按优先级排列（自带 > 目标模组 > 额外参考），最终留下的是“没有任何来源覆盖”的纯缺失内容，避免重复翻译已有内容。

---

### 第二步：`step_translate()` —— AI 翻译

这一步将第一步提取出的缺失英文文本批量交给 AI 翻译。

#### 执行流程

1. **遍历 `missing_locale/` 下的所有 `.cfg` 文件**

2. **构造 Prompt**  
   使用模板 `PROMPT_TEMPLATE`，将整个 `.cfg` 文件内容嵌入其中。  
   Prompt 中明确要求：

   - 只翻译 `=` 号右边的值
   - 保留 `[]` 内的段落名（不翻译）
   - 保留 `__1__` 这类占位符
   - 保留注释并翻译注释内容
   - **不要换行**（保持原 cfg 格式）
   - 输出纯文本，不要代码块标记

3. **调用 AI API**  
   使用配置中的 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`MODEL`，发起翻译请求。  
   额外参数 `enable_thinking: False` 用于关闭某些模型的思考链输出（保证只返回翻译结果）。

4. **后处理**  

   - `extract_code_block()`：去除 AI 可能添加的 ````cfg` 或 ````lua` 代码块标记
   - `clean_code_blocks()`：清理残留的代码块边界
   - `apply_translation_map()`：应用用户自定义的术语替换表（如将“阿巴卡伊巴”替换为“骸殖星”）

5. **输出翻译结果**  
   存入 `missing_locale_translate/` 目录，文件名与原 `.cfg` 一致。

---

### 第三步：`step_append()` —— 合并并替换原模组

这一步将翻译好的内容写回目标模组，完成“打补丁”。

#### 执行流程（分为“文件夹模式”和“zip 模式”）

**共同部分：**

- 遍历 `missing_locale_translate/` 下的所有翻译文件
- 对每个文件，读取翻译内容，准备合并到目标模组对应的 `locale/zh-CN/{同名}.cfg` 中

**文件夹模式（目标模组是文件夹）：**

1. 直接定位目标模组下的 `locale/zh-CN/` 目录，不存在则创建
2. 对每个翻译文件：
   - 如果目标 `.cfg` 已存在，**解析已有内容**，将新翻译的键值对 **合并进去**（相同键不覆盖，以已有为准）
   - 如果不存在，直接写入新文件

**ZIP 模式（目标模组是 .zip 包）：**

1. 将目标模组的 `.zip` 解压到临时目录
2. 在临时目录中定位（或创建）`locale/zh-CN/`
3. 执行与文件夹模式相同的合并逻辑
4. 将原 `.zip` 文件移到回收站（或删除）
5. 重新打包临时目录为新的 `.zip` 文件，覆盖原位置