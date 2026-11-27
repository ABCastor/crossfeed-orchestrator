package main

import (
	"context"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLoadOnlyUsesCachedFleetctlJSON(t *testing.T) {
	path := filepath.Join(t.TempDir(), "fleetctl.py")
	fixture := `
[ "$*" = '--overlay /fixture/overlay --state-dir /fixture/state glance --json --no-refresh' ] || exit 3
printf '%s' '{"schema":1,"pools":[],"subscription_spend":{"monthly":[],"unpriced":2}}'
`
	if err := os.WriteFile(path, []byte(fixture), 0o600); err != nil {
		t.Fatal(err)
	}
	c := config{python: "/bin/sh", fleetctl: path, overlay: "/fixture/overlay", state: "/fixture/state"}
	data, err := c.load(context.Background())
	if err != nil || data.Spend.Unpriced != 2 {
		t.Fatalf("read fleetctl fixture: %+v: %v", data, err)
	}
	if err := os.WriteFile(path, []byte(strings.ReplaceAll(fixture, `"schema":1`, `"schema":2`)), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := c.load(context.Background()); err == nil {
		t.Fatal("accepted unsupported schema")
	}
}

func TestCompactQuotaKeepsTheExactResetAndPace(t *testing.T) {
	used := 58
	reset := "Sat 28 Nov 11:36"
	p := pace{Words: "2% in deficit, runs out in 2d 2h"}
	if got := quota(&used, reset, p, true); got != "42% left · Sat 28 Nov 11:36 · 2% in deficit" {
		t.Fatalf("compact line: %q", got)
	}
	if got := quota(&used, reset, p, false); !strings.HasSuffix(got, "runs out in 2d 2h") {
		t.Fatalf("detailed line: %q", got)
	}
}

func TestRowsUseRemainingAndDoNotConfuseMissingWithZero(t *testing.T) {
	used := 35
	data := snapshot{
		Pools: []pool{
			{Name: "Go", Measured: true, Used: &used, Reset: "2h", Pace: pace{Words: "on pace"},
				Limits: []limit{{Title: "Weekly", Used: &used}, {Title: "5-hour"}}},
			{Name: "Argon", Measured: true},
			{Name: "Free"},
		},
		Spend: &spend{Monthly: []monthly{{Currency: "EUR", Total: "20.00"}}, Unpriced: 1},
	}
	got := rows(data)
	for i, want := range []string{"Go  65% left · 2h · on pace", "Argon  Not read", "Free  No quota window",
		"Month · EUR 20.00 · share not set · partial"} {
		if got[i].title != want {
			t.Errorf("row %d = %q, want %q", i, got[i].title, want)
		}
	}
	if len(got[0].children) != 2 || got[0].children[0] != "Weekly  65% left · reset unknown" ||
		got[0].children[1] != "5-hour  Not read" {
		t.Errorf("window rows: %v", got[0].children)
	}
}

func TestSignedLinkAcceptsOnlyTheConsolesOwnStartupLine(t *testing.T) {
	good := "Sign-in link for this launch: http://127.0.0.1:8768/login?key=abc-_123"
	if got, ok := signedLink(good); !ok || got != "http://127.0.0.1:8768/login?key=abc-_123" {
		t.Fatalf("signed link: %q %v", got, ok)
	}
	for _, line := range []string{
		"Sign-in link for this launch: https://127.0.0.1:8768/login?key=abc",     // not https
		"Sign-in link for this launch: http://evil.example:8768/login?key=abc",    // another host
		"Sign-in link for this launch: http://127.0.0.1/login?key=abc",           // no port
		"Sign-in link for this launch: http://127.0.0.1:8768/settings?key=abc",    // not the login
		"Sign-in link for this launch: http://127.0.0.1:8768/login?key=",         // empty key
		"Sign-in link for this launch: http://127.0.0.1:8768/login?key=a&b=c",     // extra parameter
		"Sign-in link for this launch: http://user:pw@127.0.0.1:8768/login?key=a", // credentials in the URL
		"http://127.0.0.1:8768/login?key=abc",                                    // not the console's line
	} {
		if got, ok := signedLink(line); ok {
			t.Errorf("accepted %q as %q", line, got)
		}
	}
}

func TestPageURLPointsTheSignedLinkAtOneLocalPage(t *testing.T) {
	link := "http://127.0.0.1:8768/login?key=abc"
	if got := pageURL(link, "/panel"); got != "http://127.0.0.1:8768/login?key=abc&next=%2Fpanel" {
		t.Fatalf("panel url: %q", got)
	}
	for _, page := range []string{"/usage", "/settings"} {
		if got := pageURL(link, page); !strings.HasSuffix(got, "next="+url.QueryEscape(page)) {
			t.Errorf("%s: %q", page, got)
		}
	}
}

func TestMenuRowsNameEachProviderOnceWithItsTightestWindow(t *testing.T) {
	used := 58
	data := snapshot{Title: "Claude 42% left", Pools: []pool{
		{Name: "Claude", Measured: true, Used: &used, Reset: "Sat 28 Nov 11:36", Pace: pace{Words: "2% in deficit, runs out in 2d 2h"},
			Limits: []limit{{Title: "Weekly", Used: &used}}},
		{Name: "Codex"},
	}}
	got := menuRows(data)
	if len(got) != 2 {
		t.Fatalf("one line per provider: %v", got)
	}
	if got[0].title != "Claude  42% left · Sat 28 Nov 11:36 · 2% in deficit" {
		t.Errorf("tightest window: %q", got[0].title)
	}
	for i, line := range got {
		if len(line.children) != 0 {
			t.Errorf("line %d has submenus; the tray shows one line per provider", i)
		}
	}
}
