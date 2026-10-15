package plugin

import (
	"regexp"
	"testing"
)

func TestVersionPatterns(t *testing.T) {
	ossRe := regexp.MustCompile(OSS_VERSION_PATTERN)
	cloudRe := regexp.MustCompile(CLOUD_VERSION_PATTERN)

	tests := []struct {
		version string
		oss     bool
		cloud   bool
	}{
		{"1.24.10", true, false},
		// the release workflow accepts tags like v1.24.10-rc.1 and packages the plugin without the "v"
		{"1.24.10-rc.1", true, false},
		{"1.24.10-openx.1", true, false},
		{"v1.24.10", false, true},
		{"github-actions-123-abc", false, true},
		{"dev-oss", false, false},
		{"1.24", false, false},
	}

	for _, tt := range tests {
		if got := ossRe.MatchString(tt.version); got != tt.oss {
			t.Errorf("OSS_VERSION_PATTERN.MatchString(%q) = %v, want %v", tt.version, got, tt.oss)
		}
		if got := cloudRe.MatchString(tt.version); got != tt.cloud {
			t.Errorf("CLOUD_VERSION_PATTERN.MatchString(%q) = %v, want %v", tt.version, got, tt.cloud)
		}
	}
}
