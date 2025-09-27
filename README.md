# Costly — OpenAI 今月のAPI利用料金ダッシュボード

## セットアップ（Windows 11 / PowerShell）
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

# 秘密鍵（Admin Key）を設定（推奨）
mkdir .streamlit
notepad .streamlit\secrets.toml
# → OPENAI_ADMIN_KEY を貼り付けて保存
