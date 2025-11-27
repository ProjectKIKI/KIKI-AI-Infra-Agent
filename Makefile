# ===========================
# KIKI AI-Infra Makefile by 아름이
# ===========================
# 사용법 예시:
#   make init        # podman / buildah 설치 (Rocky/RHEL 계열)
#   make clone       # kiki 레포 git clone
#   make build       # buildah bud로 이미지 빌드
#   make pod-up      # podman kube play kiki-pod.yaml
#   make pod-down    # podman kube down kiki-pod.yaml
#
# Makefile 실행 안됩니다. 그냥 적어둔 내용이에요.

# KIKI Git 저장소 (원하는 URL로 변경)
REPO_URL     ?= https://github.com/your-org/KIKI-AI-Infra-Agent.git
REPO_DIR     ?= KIKI-AI-Infra-Agent

# 컨테이너 이미지 이름 (로컬 레지스트리 기준)
IMAGE_AGENT  ?= localhost/kiki-ai-infra-agent:latest
IMAGE_WEB    ?= localhost/kiki-web:latest

# Containerfile 경로 (레포 안에서 상대경로) - 필요시 수정
AGENT_DFILE  ?= Containers/Containerfile.agent
WEB_DFILE    ?= kiki-web/Containerfile.web

# Podman kube play 에서 사용할 YAML
POD_YAML     ?= kiki-pod.yaml

# 기본 패키지 관리자 (Rocky/RHEL 기준)
PKG_MGR      ?= dnf

# ---- 공통 ---------------------------------------------------------

.PHONY: all init clone build build-agent build-web pod-up pod-down clean

all: clone build pod-up

# ---- 1. Podman / Buildah 설치 ------------------------------------

init:
	@echo "==> Podman / Buildah 설치 (root 권한 필요)"
	sudo $(PKG_MGR) -y install podman buildah git

# ---- 2. Git clone -------------------------------------------------

clone:
	@if [ -d "$(REPO_DIR)" ]; then \
		echo "==> $(REPO_DIR) 이미 존재함. git pull 실행..."; \
		cd "$(REPO_DIR)" && git pull --rebase; \
	else \
		echo "==> $(REPO_DIR) 없음. git clone $(REPO_URL) ..."; \
		git clone "$(REPO_URL)" "$(REPO_DIR)"; \
	fi

# ---- 3. buildah bud 이미지 빌드 -----------------------------------

build: build-agent build-web

build-agent:
	@echo "==> buildah bud: $(IMAGE_AGENT)"
	buildah bud -t "$(IMAGE_AGENT)" -f "$(AGENT_DFILE)" "$(REPO_DIR)"

build-web:
	@echo "==> buildah bud: $(IMAGE_WEB)"
	# 웹 Containerfile이 레포 안의 kiki-web/ 아래 있다고 가정
	buildah bud -t "$(IMAGE_WEB)" -f "$(WEB_DFILE)" "$(REPO_DIR)"

# ---- 4. Pod 실행 (podman kube play) -------------------------------

pod-up:
	@echo "==> podman kube play $(POD_YAML)"
	podman kube play "$(POD_YAML)"

pod-down:
	@echo "==> podman kube down $(POD_YAML)"
	podman kube down "$(POD_YAML)"

# ---- 5. 청소용 -----------------------------------------------------

clean:
	@echo "==> (옵션) 이미지/컨테이너 정리 수동 진행 권장"
	@echo "    예: podman ps -a; podman images"
