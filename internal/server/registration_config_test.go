package server

import "testing"

func TestNormalizeRegistrationConfigDefaults(t *testing.T) {
	cfg := normalizeRegistrationConfig(map[string]any{})
	if cfg["mail_provider"] != "moemail" {
		t.Fatalf("mail_provider=%v", cfg["mail_provider"])
	}
	if cfg["captcha_provider"] != "local" {
		t.Fatalf("captcha_provider=%v", cfg["captcha_provider"])
	}
	if cfg["local_solver_url"] != "http://127.0.0.1:5072" {
		t.Fatalf("local_solver_url=%v", cfg["local_solver_url"])
	}
	if cfg["proxy_strategy"] != "round_robin" {
		t.Fatalf("proxy_strategy=%v", cfg["proxy_strategy"])
	}
}

func TestIsMaskedSecret(t *testing.T) {
	if !isMaskedSecret("ab…cd") || !isMaskedSecret("****") {
		t.Fatal("expected masked")
	}
	if isMaskedSecret("real-secret-key") {
		t.Fatal("plain secret should not be masked")
	}
}

func TestSplitProxyLines(t *testing.T) {
	lines := splitProxyLines("http://a:1\n#c\nhttp://b:2;http://c:3")
	if len(lines) != 3 {
		t.Fatalf("lines=%v", lines)
	}
}
