.PHONY: build-sdk build-web build-skill build test publish-sdk install e2e clean

build-sdk:
	cd sdk/js && npm install && npm run build

build-web:
	cd web && npm install && npm run build

build-skill:
	cp skills/luma-eye/SKILL.md internal/skillasset/SKILL.md

build: build-sdk build-web build-skill
	mkdir -p bin
	CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o bin/luma-eye ./cmd/luma-eye

install: build
	mkdir -p "$(HOME)/.local/bin"
	cp bin/luma-eye "$(HOME)/.local/bin/luma-eye"
	"$(HOME)/.local/bin/luma-eye" skills install

test: build-sdk build-web
	cd sdk/js && npm test && npm run typecheck
	python3 -m unittest discover -s sdk/python/tests -p 'test_*.py'
	go test -race -v ./...

e2e: build
	bash scripts/e2e-smoke.sh

publish-sdk:
	cd sdk/js && npm test && npm run typecheck && npm publish --access public --registry https://registry.npmjs.org

clean:
	rm -rf bin
