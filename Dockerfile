FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535
WORKDIR /work
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONDONTWRITEBYTECODE=1
# 默认进行资料体检；业务验收运行：
#   docker run --rm <image> python -m unittest discover -s tests -v
#   docker run --rm <image> bash support/run_demo.sh
CMD ["python", "-m", "support.inspect_data"]
