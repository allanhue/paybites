FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY main.go ./
RUN go build -o /scanner .

FROM alpine:3.20
COPY --from=build /scanner /scanner
ENTRYPOINT ["/scanner"]
