package config

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

type Mode string

const (
	ModeAll  Mode = "all"
	ModeEdge Mode = "edge"
	ModeCore Mode = "core"
)

type Config struct {
	Mode       Mode
	ListenAddr string
	PublicURL  string
	DataDir    string

	AdminToken         string
	AdminTokenExplicit bool
	IPSalt             string

	ControlURL          string
	ControlInsecure     bool
	ControlAuthCacheTTL time.Duration
	AddonID             string

	ClickHouseURL      string
	ClickHouseDB       string
	ClickHouseUser     string
	ClickHousePassword string

	CoreURL     string
	RelaySecret string

	AutoProvisionKey      string
	AllowedOriginSuffixes []string

	FeishuWebhookURL string

	OtelEndpoint           string
	OtelHeaders            map[string]string
	OtelServiceName        string
	OtelResourceAttributes map[string]string

	BatchInterval time.Duration
	BatchSize     int
	QueueCapacity int

	RetentionDays     int
	MaxStorageBytes   int64
	RetentionInterval time.Duration
}

func LoadFromEnv() (*Config, error) {
	modeStr := strings.ToLower(strings.TrimSpace(getenv("LUMA_EYE_MODE", "all")))
	mode := Mode(modeStr)
	switch mode {
	case ModeAll, ModeEdge, ModeCore:
	default:
		return nil, fmt.Errorf("invalid LUMA_EYE_MODE %q: must be all, edge, or core", modeStr)
	}

	controlURL := strings.TrimRight(strings.TrimSpace(os.Getenv("LUMA_CONTROL_URL")), "/")
	if controlURL != "" && !strings.HasPrefix(controlURL, "https://") && !strings.HasPrefix(controlURL, "http://") {
		return nil, fmt.Errorf("invalid LUMA_CONTROL_URL %q: must start with https:// or http://", controlURL)
	}
	controlInsecure := parseBoolEnv(os.Getenv("LUMA_CONTROL_INSECURE"))
	addonID := strings.ToLower(strings.TrimSpace(getenv("LUMA_ADDON_ID", "eye")))
	if addonID == "" {
		addonID = "eye"
	}
	controlAuthCacheTTL := 60 * time.Second
	if raw := strings.TrimSpace(os.Getenv("LUMA_CONTROL_AUTH_CACHE_SECONDS")); raw != "" {
		if secs, err := strconv.ParseFloat(raw, 64); err == nil && secs >= 0 {
			controlAuthCacheTTL = time.Duration(secs * float64(time.Second))
		}
	}

	adminToken := strings.TrimSpace(os.Getenv("LUMA_EYE_ADMIN_TOKEN"))
	adminTokenExplicit := adminToken != ""
	ephemeralAdmin := false
	if adminToken == "" {
		adminToken = randomHex(24)
		ephemeralAdmin = true
	}

	ipSalt := strings.TrimSpace(os.Getenv("LUMA_EYE_IP_SALT"))
	if ipSalt == "" {
		ipSalt = adminToken
	}

	relaySecret := strings.TrimSpace(os.Getenv("LUMA_EYE_RELAY_SECRET"))
	if relaySecret == "" && mode != ModeAll {
		// In split mode, allow falling back to AdminToken only if explicitly unset in dev,
		// though production deployments should always set LUMA_EYE_RELAY_SECRET.
		relaySecret = adminToken
	} else if relaySecret == "" {
		relaySecret = randomHex(24)
	}

	suffixes := parseCSV(getenv("LUMA_EYE_AUTO_ORIGIN_SUFFIXES", "localhost,127.0.0.1"))

	batchInterval := 2 * time.Second
	if raw := strings.TrimSpace(os.Getenv("LUMA_EYE_BATCH_INTERVAL")); raw != "" {
		if d, err := time.ParseDuration(raw); err == nil && d > 0 {
			batchInterval = d
		}
	}

	retentionDays := getenvInt("LUMA_EYE_RETENTION_DAYS", 7)
	if retentionDays < 1 {
		retentionDays = 1
	}
	maxStorageBytes := parseMaxStorageBytes()
	retentionInterval := 10 * time.Minute
	if raw := strings.TrimSpace(os.Getenv("LUMA_EYE_RETENTION_INTERVAL")); raw != "" {
		if d, err := time.ParseDuration(raw); err == nil && d > 0 {
			retentionInterval = d
		}
	}

	cfg := &Config{
		Mode:                    mode,
		ListenAddr:              getenv("LUMA_EYE_LISTEN", ":8090"),
		PublicURL:               strings.TrimRight(strings.TrimSpace(os.Getenv("LUMA_EYE_PUBLIC_URL")), "/"),
		DataDir:                 getenv("LUMA_EYE_DATA_DIR", "./data"),
		AdminToken:              adminToken,
		AdminTokenExplicit:      adminTokenExplicit,
		IPSalt:                  ipSalt,
		ControlURL:              controlURL,
		ControlInsecure:         controlInsecure,
		ControlAuthCacheTTL:     controlAuthCacheTTL,
		AddonID:                 addonID,
		ClickHouseURL:           strings.TrimRight(getenv("LUMA_EYE_CLICKHOUSE_URL", "http://127.0.0.1:8123"), "/"),
		ClickHouseDB:            getenv("LUMA_EYE_CLICKHOUSE_DB", "luma_eye"),
		ClickHouseUser:          getenv("LUMA_EYE_CLICKHOUSE_USER", "default"),
		ClickHousePassword:      os.Getenv("LUMA_EYE_CLICKHOUSE_PASSWORD"),
		CoreURL:                 strings.TrimRight(strings.TrimSpace(os.Getenv("LUMA_EYE_CORE_URL")), "/"),
		RelaySecret:             relaySecret,
		AutoProvisionKey:        strings.TrimSpace(os.Getenv("LUMA_EYE_AUTO_KEY")),
		AllowedOriginSuffixes:   suffixes,
		FeishuWebhookURL:        strings.TrimSpace(os.Getenv("LUMA_EYE_FEISHU_WEBHOOK")),
		OtelEndpoint:            strings.TrimRight(strings.TrimSpace(os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")), "/"),
		OtelHeaders:             parseKeyValuePairs(os.Getenv("OTEL_EXPORTER_OTLP_HEADERS")),
		OtelServiceName:         getenv("OTEL_SERVICE_NAME", "luma-eye"),
		OtelResourceAttributes:  parseKeyValuePairs(os.Getenv("OTEL_RESOURCE_ATTRIBUTES")),
		BatchInterval:           batchInterval,
		BatchSize:               getenvInt("LUMA_EYE_BATCH_SIZE", 2000),
		QueueCapacity:           getenvInt("LUMA_EYE_QUEUE_CAP", 32768),
		RetentionDays:           retentionDays,
		MaxStorageBytes:         maxStorageBytes,
		RetentionInterval:       retentionInterval,
	}

	if cfg.Mode == ModeEdge && cfg.CoreURL == "" {
		return nil, fmt.Errorf("LUMA_EYE_CORE_URL is required when LUMA_EYE_MODE=edge")
	}

	if ephemeralAdmin && cfg.ControlURL == "" {
		fmt.Fprintf(os.Stderr, "[luma-eye] WARNING: LUMA_EYE_ADMIN_TOKEN not set; generated ephemeral dev token: %s\n", adminToken)
	}

	return cfg, nil
}

func parseBoolEnv(raw string) bool {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}

func getenv(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

func getenvInt(key string, fallback int) int {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil || n <= 0 {
		return fallback
	}
	return n
}

func parseCSV(raw string) []string {
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	parts := strings.Split(raw, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

func parseKeyValuePairs(raw string) map[string]string {
	out := make(map[string]string)
	for _, item := range strings.Split(raw, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		k, v, ok := strings.Cut(item, "=")
		if !ok {
			continue
		}
		k = strings.TrimSpace(k)
		v = strings.TrimSpace(v)
		if decoded, err := url.QueryUnescape(v); err == nil {
			v = decoded
		}
		if k != "" {
			out[k] = v
		}
	}
	return out
}

func parseMaxStorageBytes() int64 {
	const defaultCap = int64(15) << 30 // 15 GiB
	if raw := strings.TrimSpace(os.Getenv("LUMA_EYE_MAX_STORAGE_BYTES")); raw != "" {
		n, err := strconv.ParseInt(raw, 10, 64)
		if err == nil {
			if n <= 0 {
				return 0
			}
			return n
		}
	}
	if raw := strings.TrimSpace(os.Getenv("LUMA_EYE_MAX_STORAGE_GB")); raw != "" {
		gb, err := strconv.ParseFloat(raw, 64)
		if err == nil {
			if gb <= 0 {
				return 0
			}
			return int64(gb * float64(int64(1)<<30))
		}
	}
	return defaultCap
}

func randomHex(nBytes int) string {
	b := make([]byte, nBytes)
	if _, err := rand.Read(b); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(b)
}
