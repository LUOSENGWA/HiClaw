package server

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	authpkg "github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/auth"
)

func TestCodingCliAuthenticationHeadersRedacted(t *testing.T) {
	settings := `{"provider":{"custom":{"options":{"headers":{"Authorization":"Bearer synthetic-bearer","pRoXy-AuThOrIzAtIoN":"Basic synthetic-proxy","Cookie":"session=synthetic-cookie","X-Api-Key":"synthetic-api-key","User-Agent":"ordinary-user-agent"}}}}}`
	for _, method := range []string{http.MethodGet, http.MethodPut} {
		t.Run(method, func(t *testing.T) {
			exec := &fakeExecBackend{fileWrites: map[string]string{}}
			exec.respond = func(cmd []string) (string, string, int, error) {
				if strings.Contains(cmd[2], "cat ") {
					return settings, "", 0, nil
				}
				return "/root", "", 0, nil
			}
			h := newTestCodingCliHandler(t, "embedded", exec, nil, checkpointTeamWithWorkers("team-a", "daily-carol")...)
			req := codingCliRequest(method, "/settings", settings, "name", "daily-carol", "cli", "opencode")
			rec := httptest.NewRecorder()
			if method == http.MethodGet {
				req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleHuman, Username: "review-human", Teams: []string{"team-a"}})
				h.getCliSettings(rec, req)
			} else {
				h.putCliSettings(rec, adminCaller(req))
			}
			if rec.Code != http.StatusOK {
				t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
			}
			for _, secret := range []string{"synthetic-bearer", "synthetic-proxy", "synthetic-cookie", "synthetic-api-key"} {
				if strings.Contains(rec.Body.String(), secret) {
					t.Errorf("response exposed authentication header %q", secret)
				}
			}
			if !strings.Contains(rec.Body.String(), "ordinary-user-agent") {
				t.Fatal("non-sensitive header was removed")
			}
			if method == http.MethodPut {
				written, _ := exec.written("/root/.config/opencode/opencode.json")
				if !strings.Contains(written, "synthetic-bearer") {
					t.Fatal("persisted credentials must remain usable by the CLI")
				}
			}
		})
	}
}

func TestCodingCliConcurrentSettingsConflictAndRetry(t *testing.T) {
	const settingsPath = "/root/.qwen/settings.json"
	exec := &fakeExecBackend{fileWrites: map[string]string{settingsPath: `{"base":1}`}}
	firstRead, release, done := make(chan struct{}), make(chan struct{}), make(chan struct{})
	var reads atomic.Int32
	exec.respond = func(cmd []string) (string, string, int, error) {
		if strings.Contains(cmd[2], "cat ") {
			if strings.Contains(cmd[2], ".qwen/settings.json") {
				current, _ := exec.written(settingsPath)
				if reads.Add(1) == 1 {
					close(firstRead)
					<-release
				}
				return current, "", 0, nil
			}
			return `{}`, "", 0, nil
		}
		return "/root", "", 0, nil
	}
	h := newTestCodingCliHandler(t, "embedded", exec, nil, checkpointTeamWithWorkers("team-a", "daily-carol")...)
	put := func(cli, body string) *httptest.ResponseRecorder {
		rec := httptest.NewRecorder()
		h.putCliSettings(rec, adminCaller(codingCliRequest(http.MethodPut, "/settings", body, "name", "daily-carol", "cli", cli)))
		return rec
	}
	first := httptest.NewRecorder()
	go func() {
		defer close(done)
		h.putCliSettings(first, adminCaller(codingCliRequest(http.MethodPut, "/settings", `{"a":1}`, "name", "daily-carol", "cli", "qwen-code")))
	}()
	defer func() {
		select {
		case <-release:
		default:
			close(release)
		}
		<-done
	}()
	select {
	case <-firstRead:
	case <-time.After(2 * time.Second):
		t.Fatal("first update did not start")
	}
	if rec := put("qwen-code", `{"b":2}`); rec.Code != http.StatusConflict {
		t.Fatalf("concurrent update status=%d, want 409", rec.Code)
	}
	if rec := put("opencode", `{"independent":true}`); rec.Code != http.StatusOK {
		t.Fatalf("unrelated CLI status=%d, want 200", rec.Code)
	}
	close(release)
	<-done
	if first.Code != http.StatusOK {
		t.Fatalf("first update status=%d", first.Code)
	}
	if rec := put("qwen-code", `{"b":2}`); rec.Code != http.StatusOK {
		t.Fatalf("retry status=%d", rec.Code)
	}
	written, _ := exec.written(settingsPath)
	doc := decodeJSON(t, written)
	if doc["base"] != float64(1) || doc["a"] != float64(1) || doc["b"] != float64(2) {
		t.Fatalf("retry lost an update: %s", written)
	}
}

func TestCodingCliSettingsBusyReleasedAfterWriteFailure(t *testing.T) {
	exec := &fakeExecBackend{fileWrites: map[string]string{}, writeErr: errors.New("test write failure")}
	exec.respond = func(cmd []string) (string, string, int, error) {
		if strings.Contains(cmd[2], "cat ") {
			return `{}`, "", 0, nil
		}
		return "/root", "", 0, nil
	}
	h := newTestCodingCliHandler(t, "embedded", exec, nil, checkpointTeamWithWorkers("team-a", "daily-carol")...)
	put := func() int {
		rec := httptest.NewRecorder()
		h.putCliSettings(rec, adminCaller(codingCliRequest(http.MethodPut, "/settings", `{"a":1}`, "name", "daily-carol", "cli", "qwen-code")))
		return rec.Code
	}
	if code := put(); code != http.StatusBadGateway {
		t.Fatalf("failed write status=%d", code)
	}
	exec.writeErr = nil
	if code := put(); code != http.StatusOK {
		t.Fatalf("busy gate not released after error, status=%d", code)
	}
}

func TestCodingCliSettingsReadTimeoutDoesNotOverwrite(t *testing.T) {
	const path = "/root/.qwen/settings.json"
	exec := &fakeExecBackend{fileWrites: map[string]string{path: `{"keep":true}`}}
	exec.respond = func(cmd []string) (string, string, int, error) {
		return "", "", 137, errors.New("worker command timed out or was killed (exit 137)")
	}
	h := newTestCodingCliHandler(t, "embedded", exec, nil, checkpointTeamWithWorkers("team-a", "daily-carol")...)
	for _, method := range []string{http.MethodGet, http.MethodPut} {
		rec := httptest.NewRecorder()
		req := adminCaller(codingCliRequest(method, "/settings", `{"new":true}`, "name", "daily-carol", "cli", "qwen-code"))
		if method == http.MethodGet {
			h.getCliSettings(rec, req)
		} else {
			h.putCliSettings(rec, req)
		}
		if rec.Code != http.StatusBadGateway {
			t.Fatalf("%s timeout status=%d, want 502", method, rec.Code)
		}
	}
	if got, _ := exec.written(path); got != `{"keep":true}` {
		t.Fatalf("timeout overwrote existing settings: %s", got)
	}
}
