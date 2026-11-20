// Package geoip resolves coarse country codes locally. No lookup sends visitor IPs elsewhere.
package geoip

import (
	"bytes"
	"compress/gzip"
	_ "embed"
	"io"
	"net"
	"strings"
	"sync"

	"github.com/oschwald/maxminddb-golang"
)

// DB-IP Country Lite, CC BY 4.0. See NOTICE.md for attribution and refresh instructions.
//
//go:embed country.mmdb.gz
var compressed []byte

var once sync.Once
var reader *maxminddb.Reader

func Country(raw string) string {
	ip := net.ParseIP(strings.TrimSpace(raw))
	if ip == nil || ip.IsPrivate() || ip.IsLoopback() || ip.IsUnspecified() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip.IsMulticast() {
		return ""
	}
	once.Do(func() {
		z, err := gzip.NewReader(bytes.NewReader(compressed))
		if err != nil {
			return
		}
		defer z.Close()
		data, err := io.ReadAll(io.LimitReader(z, 64<<20))
		if err != nil {
			return
		}
		reader, _ = maxminddb.FromBytes(data)
	})
	if reader == nil {
		return ""
	}
	var record struct {
		Country struct {
			ISOCode string `maxminddb:"iso_code"`
		} `maxminddb:"country"`
	}
	if reader.Lookup(ip, &record) != nil {
		return ""
	}
	if len(record.Country.ISOCode) != 2 {
		return ""
	}
	return strings.ToUpper(record.Country.ISOCode)
}

var tzCountryMap = map[string]string{
	"asia/shanghai":       "CN",
	"asia/chongqing":      "CN",
	"asia/harbin":         "CN",
	"asia/urumqi":         "CN",
	"prc":                 "CN",
	"asia/hong_kong":      "HK",
	"asia/macau":          "MO",
	"asia/taipei":         "TW",
	"asia/tokyo":          "JP",
	"asia/seoul":          "KR",
	"asia/singapore":      "SG",
	"asia/kuala_lumpur":   "MY",
	"asia/bangkok":        "TH",
	"asia/ho_chi_minh":    "VN",
	"asia/saigon":         "VN",
	"asia/jakarta":        "ID",
	"asia/manila":         "PH",
	"asia/kolkata":        "IN",
	"asia/calcutta":       "IN",
	"asia/dubai":          "AE",
	"europe/london":       "GB",
	"europe/berlin":       "DE",
	"europe/paris":        "FR",
	"europe/amsterdam":    "NL",
	"europe/madrid":       "ES",
	"europe/rome":         "IT",
	"europe/zurich":       "CH",
	"europe/stockholm":    "SE",
	"europe/warsaw":       "PL",
	"europe/moscow":       "RU",
	"america/new_york":    "US",
	"america/chicago":     "US",
	"america/denver":      "US",
	"america/los_angeles": "US",
	"america/phoenix":     "US",
	"america/anchorage":   "US",
	"pacific/honolulu":    "US",
	"america/toronto":     "CA",
	"america/vancouver":   "CA",
	"america/sao_paulo":   "BR",
	"america/mexico_city": "MX",
	"australia/sydney":    "AU",
	"australia/melbourne": "AU",
	"pacific/auckland":    "NZ",
}

// CountryFromTimezone resolves a zero-PII country hint from an IANA timezone when IP is private/unknown.
func CountryFromTimezone(tz string) string {
	low := strings.ToLower(strings.TrimSpace(tz))
	if low == "" {
		return ""
	}
	return tzCountryMap[low]
}

// Resolve prioritizes edge Geo headers, then embedded offline DB-IP lookup, then timezone fallback.
func Resolve(headerCountry, clientIP, tz string) string {
	if hc := strings.ToUpper(strings.TrimSpace(headerCountry)); len(hc) == 2 && hc != "XX" {
		return hc
	}
	if cc := Country(clientIP); cc != "" {
		return cc
	}
	return CountryFromTimezone(tz)
}
