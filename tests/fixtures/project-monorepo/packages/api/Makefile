.PHONY: lint test coverage ci

lint:
	golangci-lint run ./...

test:
	go test -race ./...

coverage:
	go test -coverprofile=cover.out ./...

ci: lint test
