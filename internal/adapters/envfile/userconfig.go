package envfile

import (
	"bytes"
	"encoding/json"
	"fmt"
)

func UserConfig(data []byte) (map[string]string, error) {
	if len(data) > MaxBytes {
		return nil, fmt.Errorf("user config exceeds size limit")
	}
	if !json.Valid(data) {
		return nil, fmt.Errorf("invalid user config JSON")
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	var raw map[string]any
	if err := decoder.Decode(&raw); err != nil || raw == nil {
		return nil, fmt.Errorf("user config must be a JSON object")
	}
	if env, exists := raw["env"]; exists {
		var ok bool
		raw, ok = env.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("user config env must be an object")
		}
	}
	values := map[string]string{}
	for key, value := range raw {
		if value == nil {
			continue
		}
		if !namePattern.MatchString(key) {
			return nil, fmt.Errorf("invalid env name in user config")
		}
		switch v := value.(type) {
		case string:
			values[key] = v
		case bool:
			values[key] = "False"
			if v {
				values[key] = "True"
			}
		case json.Number:
			values[key] = v.String()
		default:
			return nil, fmt.Errorf("user config values must be scalar")
		}
	}
	return values, nil
}
