# PROXY
python3.13 -m venv venv

source venv/bin/activate

pip install -r requirements.txt

uvicorn api.index:app --host 0.0.0.0 --port 5001
