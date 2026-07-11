package main

import (
	"context"
	"net/http"
	"os"
	"time"
)

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://127.0.0.1:9000/minio/health/ready", nil)
	if err != nil {
		os.Exit(1)
	}
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		os.Exit(1)
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		os.Exit(1)
	}
}
