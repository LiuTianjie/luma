package alert

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/liutianjie/luma-eye/internal/model"
)

type FeishuNotifier struct {
	webhookURL string
	publicURL  string
	httpClient *http.Client

	mu       sync.Mutex
	lastSent map[string]time.Time
}

func NewFeishuNotifier(webhookURL, publicURL string) *FeishuNotifier {
	return &FeishuNotifier{
		webhookURL: strings.TrimSpace(webhookURL),
		publicURL:  strings.TrimRight(strings.TrimSpace(publicURL), "/"),
		httpClient: &http.Client{Timeout: 8 * time.Second},
		lastSent:   make(map[string]time.Time),
	}
}

func (f *FeishuNotifier) Enabled() bool {
	return f != nil && f.webhookURL != ""
}

func (f *FeishuNotifier) NotifyIssueAsync(reason string, exc model.ExceptionEvent) {
	if !f.Enabled() {
		return
	}
	key := exc.Project + "\x00" + exc.Fingerprint + "\x00" + reason
	now := time.Now()

	f.mu.Lock()
	if prev, ok := f.lastSent[key]; ok && now.Sub(prev) < 60*time.Second {
		f.mu.Unlock()
		return
	}
	f.lastSent[key] = now
	f.mu.Unlock()

	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
		defer cancel()
		_ = f.sendCard(ctx, reason, exc)
	}()
}

func (f *FeishuNotifier) sendCard(ctx context.Context, reason string, exc model.ExceptionEvent) error {
	titleTag := "新异常"
	templateColor := "red"
	if reason == "regression" {
		titleTag = "回归异常"
		templateColor = "orange"
	}
	title := fmt.Sprintf("[Luma Eye · %s] %s · %s", titleTag, exc.Project, exc.ErrorType)

	location := exc.Culprit
	if location == "" {
		location = exc.PageURL
	}
	if location == "" {
		location = "-"
	}
	env := exc.Env
	if env == "" {
		env = "production"
	}
	rel := exc.Release
	if rel == "" {
		rel = "-"
	}
	msg := exc.ErrorMessage
	if len(msg) > 300 {
		msg = msg[:300] + "..."
	}

	elements := []map[string]any{
		{
			"tag": "div",
			"text": map[string]any{
				"tag":     "lark_md",
				"content": fmt.Sprintf("**错误详情：** %s\n**定位：** `%s`\n**环境 / 版本：** `%s` / `%s`\n**页面：** %s\n**指纹：** `%s`", escapeMarkdown(msg), location, env, rel, exc.PageURL, exc.Fingerprint),
			},
		},
	}
	if f.publicURL != "" {
		link := fmt.Sprintf("%s/console/?project=%s&tab=issues&issue=%s", f.publicURL, exc.Project, exc.Fingerprint)
		elements = append(elements, map[string]any{
			"tag": "action",
			"actions": []map[string]any{
				{
					"tag":  "button",
					"text": map[string]any{"tag": "plain_text", "content": "在 Luma Eye 查看堆栈与面包屑"},
					"type": "primary",
					"url":  link,
				},
			},
		})
	}

	payload := map[string]any{
		"msg_type": "interactive",
		"card": map[string]any{
			"header": map[string]any{
				"template": templateColor,
				"title": map[string]any{
					"tag":     "plain_text",
					"content": title,
				},
			},
			"elements": elements,
		},
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, f.webhookURL, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := f.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode >= 300 {
		return fmt.Errorf("feishu webhook status %d", resp.StatusCode)
	}
	return nil
}

func escapeMarkdown(s string) string {
	s = strings.ReplaceAll(s, "\n", " ")
	return strings.TrimSpace(s)
}
