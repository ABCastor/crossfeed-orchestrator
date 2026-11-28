package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fixture is the document the panel's own tests and browser flows draw: both sides must title it the same way.
func fixture(t *testing.T) barView {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "tests", "fixtures", "panel-r12.json"))
	if err != nil {
		t.Fatal(err)
	}
	return decodeBar(raw)
}

func TestTitleFollowsTheChoicesTheWebPanelPreviews(t *testing.T) {
	view := fixture(t)
	now := time.Date(2026, 11, 28, 10, 0, 0, 0, time.UTC)
	cases := []struct {
		name  string
		prefs barPrefs
		text  string
		icon  string
		logo  string
	}{
		{"defaults: the most pressed allowance, the window and the figure", barPrefs{}, "Week 71%", "provider", "openai"},
		{"full text names the provider", barPrefs{Text: "full"}, "Codex Week 71%", "provider", "openai"},
		{"the number alone, counted down", barPrefs{Text: "number", Count: "left"}, "29% left", "provider", "openai"},
		{"a chosen provider", barPrefs{Show: "claude", Text: "full"}, "Claude Week 58%", "provider", "claude"},
		{"a chosen window", barPrefs{Window: "5h", Text: "full"}, "Claude 5h 41%", "provider", "claude"},
		{"pace after the number", barPrefs{Extra: "pace"}, "Week 71% · 48% over", "provider", "openai"},
		{"cost after the number sums every provider's day", barPrefs{Extra: "cost"}, "Week 71% · US$11.68 today", "provider", "openai"},
		{"no text keeps the logo", barPrefs{Text: "none"}, "", "provider", "openai"},
		{"the Crossfeed mark", barPrefs{Icon: "crossfeed"}, "Week 71%", "crossfeed", "openai"},
		{"no icon, still a text", barPrefs{Icon: "none"}, "Week 71%", "none", "openai"},
		{"no icon and no text would be invisible", barPrefs{Icon: "none", Text: "none"}, "", "crossfeed", "openai"},
		{"a provider with no reading falls back to the most pressed", barPrefs{Show: "openrouter-free", Text: "number"}, "71%", "provider", "openai"},
		{"an unknown value is the default", barPrefs{Icon: "tanks", Text: "loud", Count: "sideways"}, "Week 71%", "provider", "openai"},
	}
	for _, c := range cases {
		got := barTitle(view, c.prefs, now)
		if got.Text != c.text || got.Icon != c.icon || got.Logo != c.logo {
			t.Errorf("%s: got text %q icon %q logo %q, want %q %q %q", c.name, got.Text, got.Icon, got.Logo, c.text, c.icon, c.logo)
		}
	}
}

func TestTitleResetUsesTheNextResetOfTheShownWindow(t *testing.T) {
	view := fixture(t)
	generated := time.Now()
	got := barTitle(view, barPrefs{Show: "claude", Text: "number", Extra: "reset"}, generated)
	if !strings.HasPrefix(got.Text, "58% · ") {
		t.Fatalf("reset after the number: %q", got.Text)
	}
	if barTitle(view, barPrefs{Extra: "reset"}, generated).Text != "Week 71%" {
		t.Fatal("a window with no reset clock adds nothing")
	}
}

func TestEveryProviderLogoIsEmbeddedAndTheTitleNeverNamesOneThatIsNot(t *testing.T) {
	for _, name := range logos {
		if len(logoPNG(name)) < 100 {
			t.Errorf("no embedded logo for %s", name)
		}
	}
	if logoPNG("../main") != nil || logoPNG("") != nil {
		t.Fatal("a logo name is a file name in icons/ and nothing else")
	}
}

func TestNoReadingIsAnIconAndAWord(t *testing.T) {
	got := barTitle(barView{}, barPrefs{}, time.Now())
	if got.Text != "" || got.Icon != "crossfeed" || got.Tooltip == "" {
		t.Fatalf("empty glance: %+v", got)
	}
	if barTitle(barView{}, barPrefs{Icon: "none"}, time.Now()).Icon != "none" {
		t.Fatal("an operator who turned the icon off stays without one")
	}
}

func TestPreferencesCarryTheRefreshIntervalAndWhereToOpen(t *testing.T) {
	var p panelPrefs
	if p.everyMinutes() != 5 {
		t.Fatalf("default interval: %d", p.everyMinutes())
	}
	zero, fifteen, seven, day, past := 0, 15, 7, 1440, 1441
	p.Refresh.EveryMinutes = &zero
	if p.everyMinutes() != 0 {
		t.Fatal("0 means off")
	}
	p.Refresh.EveryMinutes = &fifteen
	if p.everyMinutes() != 15 {
		t.Fatal("15 minutes")
	}
	p.Refresh.EveryMinutes = &seven
	if p.everyMinutes() != 7 {
		t.Fatal("any whole number of minutes is an interval")
	}
	p.Refresh.EveryMinutes = &day
	if p.everyMinutes() != 1440 {
		t.Fatal("a day is the longest interval")
	}
	p.Refresh.EveryMinutes = &past
	if p.everyMinutes() != 5 {
		t.Fatal("past a day is the default")
	}
	p.OpenOn = "last"
	if p.barFragment() != "" {
		t.Fatalf("last used sends no tab, the page opens on its own memory: %q", p.barFragment())
	}
	p.OpenOn = "codex"
	if p.barFragment() != "tab=codex" {
		t.Fatalf("open on a named tab: %q", p.barFragment())
	}
	p.OpenOn = "../x"
	if p.barFragment() != "" {
		t.Fatal("a tab id is a plain id, never a path")
	}
}
