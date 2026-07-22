FROM golang:1.24-alpine AS builder

WORKDIR /src
COPY entrypoint.go .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /transfer-entrypoint entrypoint.go

FROM dutchcoders/transfer.sh:latest
COPY --from=builder /transfer-entrypoint /go/bin/transfer-entrypoint

ENTRYPOINT ["/go/bin/transfer-entrypoint"]
