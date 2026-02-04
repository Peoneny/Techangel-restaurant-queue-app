FROM python:3.10-slim

WORKDIR /app

COPY requirements_enhanced.txt .
RUN pip install --no-cache-dir -r requirements_enhanced.txt

COPY . .

EXPOSE 8080

CMD ["python", "app_with_auth.py"]
