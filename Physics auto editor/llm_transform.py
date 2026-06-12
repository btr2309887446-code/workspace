import json
import os
import html
import requests # 这里用 requests 避免你需要额外安装特定 SDK，但你也可以换成 openai 包

# ================= 配置区域 =================
# 请在此处填入你的大模型 API KEY 和接口地址
API_KEY = os.environ.get("OPENAI_API_KEY", "your_api_key_here") 
BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1/chat/completions") 
MODEL_NAME = "gpt-4o" # 使用的模型名称，可根据你的供应商更改 (如 qwen-max, deepseek-chat 等)

INPUT_FILE = "Q1_problem.json"
OUTPUT_HTML = "output.html"
OUTPUT_JSON = "Q1_problem_updated.json"
# ============================================

def call_llm(prompt):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}"
    }
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7
    }
    try:
        response = requests.post(BASE_URL, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f"调用LLM出错: {str(e)}")
        if 'response' in locals() and hasattr(response, 'text'):
            print(f"响应内容: {response.text}")
        return f"调用LLM出错: {str(e)}"

def process_problems():
    print(f"正在读取文件: {INPUT_FILE}")
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    total_problems = len(data)
    print(f"总共识别到 {total_problems} 道问题，开始处理...")
    
    results = []
    
    for idx, item in enumerate(data):
        context = item.get("题目信息", {}).get("上下文", "")
        original_problem = item.get("题目信息", {}).get("问题原文", "")
        
        print(f"正在处理第 {idx + 1}/{total_problems} 道题目...")
        
        prompt = f"""你是一个专业的物理出题专家。请根据以下提供的物理题目的“上下文信息”和“问题原文”，将问题改造成“选择题”和“填空题”两种形式。

上下文信息：
{context}

问题原文：
{original_problem}

要求：
1. 结合上下文进行问题的更正，确保物理逻辑和背景连贯。
2. 将“问题原文”分别改造成一道选择题和一道填空题，包含在这一个回答中。
3. 格式要求：
   - 必须将真实的换行替换为字面量字符 '\\n' (即反斜杠加字母n)
   - 公式内的反斜杠必须为双斜杠 '\\\\' (例如将 \\frac 写为 \\\\frac)
   - 其他文字表达与“问题原文”尽量保持完全一致
4. 只输出改造后的最终文本内容，不要输出任何额外的解释和 Markdown 代码块包裹符号（如 ```）。
"""
        
        # 为了演示和未配置API Key的情况，如果 API KEY 是默认值，跳过真实请求
        if API_KEY == "your_api_key_here":
            print("注意：未配置 API KEY，跳过 LLM 调用，生成演示数据。请修改代码中的 API_KEY。")
            transformed = f"【演示数据】\\n选择题：{original_problem} A... B... C... D...\\n填空题：{original_problem} ____"
        else:
            transformed = call_llm(prompt)
            # 过滤可能的 markdown 代码块
            if transformed.startswith("```"):
                lines = transformed.split("\n")
                if len(lines) > 2:
                    transformed = "\n".join(lines[1:-1])
            
        # 更新 JSON 数据
        if "题目信息" in item:
            item["题目信息"]["改造后问题"] = transformed
            
        results.append({
            "id": idx + 1,
            "original": original_problem,
            "transformed": transformed
        })

    # 保存新的 JSON
    with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"已保存更新后的 JSON 文件: {OUTPUT_JSON}")

    # 生成 HTML
    generate_html(results, total_problems)

def generate_html(results, total_problems):
    html_template = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>物理题目改造结果</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; padding: 20px; background-color: #f4f7f6; color: #333; }}
        h1 {{ text-align: center; color: #2c3e50; }}
        .summary {{ text-align: center; font-size: 1.2em; margin-bottom: 30px; }}
        .problem-card {{ background: white; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); margin-bottom: 25px; padding: 20px; }}
        .problem-header {{ border-bottom: 2px solid #ecf0f1; padding-bottom: 10px; margin-bottom: 15px; color: #2980b9; }}
        .content-row {{ display: flex; flex-direction: column; gap: 10px; margin-bottom: 15px; }}
        .text-box {{ background: #f8f9fa; border: 1px solid #e9ecef; border-left: 4px solid #3498db; padding: 15px; border-radius: 4px; white-space: pre-wrap; font-family: monospace; font-size: 14px; overflow-x: auto; }}
        button {{ align-self: flex-start; cursor: pointer; padding: 8px 16px; background-color: #2ecc71; color: white; border: none; border-radius: 4px; font-weight: bold; transition: background-color 0.3s; }}
        button:hover {{ background-color: #27ae60; }}
        .label {{ font-weight: bold; color: #34495e; }}
    </style>
    <script>
        function copyText(button, textId) {{
            const textElement = document.getElementById(textId);
            const text = textElement.innerText || textElement.textContent;
            navigator.clipboard.writeText(text).then(() => {{
                const originalText = button.innerText;
                button.innerText = '✅ 已复制!';
                button.style.backgroundColor = '#f39c12';
                setTimeout(() => {{ 
                    button.innerText = originalText; 
                    button.style.backgroundColor = '#2ecc71';
                }}, 2000);
            }}).catch(err => {{
                console.error('复制失败:', err);
                alert('复制失败，请手动复制');
            }});
        }}
    </script>
</head>
<body>
    <h1>物理题目自动改造批处理</h1>
    <div class="summary">共识别到 <strong>{total_problems}</strong> 道问题。</div>
"""

    for res in results:
        html_template += f"""
    <div class="problem-card">
        <h2 class="problem-header">问题 {res['id']}</h2>
        
        <div class="content-row">
            <span class="label">问题原文:</span>
            <div class="text-box" id="orig_{res['id']}">{html.escape(res['original'])}</div>
            <button onclick="copyText(this, 'orig_{res['id']}')">📋 一键复制原文</button>
        </div>
        
        <div class="content-row">
            <span class="label">改造后问题 (选择题 & 填空题):</span>
            <div class="text-box" id="trans_{res['id']}">{html.escape(res['transformed'])}</div>
            <button onclick="copyText(this, 'trans_{res['id']}')">📋 一键复制改造后内容</button>
        </div>
    </div>
"""

    html_template += """
</body>
</html>
"""

    with open(OUTPUT_HTML, 'w', encoding='utf-8') as f:
        f.write(html_template)
    print(f"已生成可视化结果文件: {OUTPUT_HTML}")

if __name__ == "__main__":
    process_problems()
