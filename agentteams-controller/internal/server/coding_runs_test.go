package server

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	v1beta1 "github.com/agentscope-ai/AgentTeams/agentteams-controller/api/v1beta1"
	authpkg "github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/auth"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/oss/ossfake"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

// newCodingRunsTestHandler builds a CodingRunsHandler over an in-memory mc-like
// store and a fake K8s client holding the given Teams (reuses the project test
// scheme / mcLikeOSS directory-view wrapper / team helper).
func newCodingRunsTestHandler(t *testing.T, store *ossfake.Memory, teams ...*v1beta1.Team) *CodingRunsHandler {
	t.Helper()
	objs := make([]runtime.Object, 0, len(teams))
	for _, tm := range teams {
		objs = append(objs, tm)
	}
	k8s := fake.NewClientBuilder().WithScheme(newProjectTestScheme(t)).WithRuntimeObjects(objs...).Build()
	return NewCodingRunsHandler(k8s, "default", &mcLikeOSS{Memory: store})
}

func putRun(store *ossfake.Memory, key, data string) {
	_ = store.PutObject(context.Background(), key, []byte(data))
}

const (
	teamRunKey  = "teams/alpha-team/shared/tasks/t1/workspace/coding-cli-logs/run-20261012-120000.log"
	standRunKey = "shared/tasks/t9/workspace/coding-cli-logs/run-20261012-120100.log"
	betaRunKey  = "teams/beta-team/shared/tasks/t2/workspace/coding-cli-logs/run-20261012-120200.log"
)

func TestListCodingRuns_AdminSeesTeamAndStandalone(t *testing.T) {
	store := ossfake.NewMemory()
	putRun(store, teamRunKey, "alpha t1 run")
	putRun(store, standRunKey, "standalone t9 run")
	putRun(store, betaRunKey, "beta t2 run")
	h := newCodingRunsTestHandler(t, store, team("alpha-team"), team("beta-team"))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/coding-runs", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.ListCodingRuns(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	var runs []codingRun
	if err := json.Unmarshal(rec.Body.Bytes(), &runs); err != nil {
		t.Fatalf("decode list: %v", err)
	}
	// Admin sees team (alpha, beta) + standalone runs: 3.
	if len(runs) != 3 {
		t.Fatalf("admin runs = %d, want 3: %+v", len(runs), runs)
	}
}

func TestListCodingRuns_L2SeesOnlyOwnTeam(t *testing.T) {
	store := ossfake.NewMemory()
	putRun(store, teamRunKey, "alpha t1 run")
	putRun(store, standRunKey, "standalone t9 run")
	putRun(store, betaRunKey, "beta t2 run")
	h := newCodingRunsTestHandler(t, store, team("alpha-team"), team("beta-team"))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/coding-runs", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleTeamLeader, Username: "alpha-lead", Team: "alpha-team"})
	rec := httptest.NewRecorder()
	h.ListCodingRuns(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var runs []codingRun
	_ = json.Unmarshal(rec.Body.Bytes(), &runs)
	// L2 alpha-leader sees only the alpha-team run; standalone (no team) and
	// beta-team run are excluded.
	if len(runs) != 1 {
		t.Fatalf("L2 runs = %d, want 1 (alpha only): %+v", len(runs), runs)
	}
	if runs[0].TaskID != "t1" || runs[0].Team != "alpha-team" {
		t.Fatalf("L2 run = %+v, want task t1 team alpha-team", runs[0])
	}
}

func TestListCodingRuns_SkipsNonRunFiles(t *testing.T) {
	store := ossfake.NewMemory()
	putRun(store, teamRunKey, "a run")
	putRun(store, "teams/alpha-team/shared/tasks/t1/workspace/coding-cli-logs/.gitkeep", "x")
	putRun(store, "teams/alpha-team/shared/tasks/t1/workspace/coding-cli-logs/run-notes.txt", "y")
	h := newCodingRunsTestHandler(t, store, team("alpha-team"))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/coding-runs", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.ListCodingRuns(rec, req)

	var runs []codingRun
	_ = json.Unmarshal(rec.Body.Bytes(), &runs)
	if len(runs) != 1 || runs[0].Run != "run-20261012-120000.log" {
		t.Fatalf("runs = %+v, want only the well-formed run", runs)
	}
}

func getRun(h *CodingRunsHandler, taskID, run string, caller *authpkg.CallerIdentity) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, "/api/v1/coding-runs/"+taskID+"/runs/"+run, nil)
	req.SetPathValue("taskID", taskID)
	req.SetPathValue("run", run)
	if caller != nil {
		req = withCaller(req, caller)
	}
	rec := httptest.NewRecorder()
	h.GetCodingRun(rec, req)
	return rec
}

func TestGetCodingRun_AdminReadsByteTransparent(t *testing.T) {
	store := ossfake.NewMemory()
	body := "[run-coding-cli] cli=qwen ...\n<full transcript>"
	putRun(store, teamRunKey, body)
	h := newCodingRunsTestHandler(t, store, team("alpha-team"))

	rec := getRun(h, "t1", "run-20261012-120000.log", &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if got := rec.Body.String(); got != body {
		t.Fatalf("body = %q, want byte-transparent %q", got, body)
	}
	// Default (no ?format) stays byte-transparent text/plain.
	if ct := rec.Header().Get("Content-Type"); ct != "text/plain; charset=utf-8" {
		t.Fatalf("content-type = %q, want text/plain; charset=utf-8 (default unchanged)", ct)
	}
}

const (
	runsJSONLKey = "teams/alpha-team/shared/tasks/t1/workspace/coding-cli-logs/runs.jsonl"
	promptKey    = "teams/alpha-team/shared/tasks/t1/coding-prompts/20261012-120000.txt"
)

// fullRecordLine is a runs.jsonl line (the #1342 wrapper's schema) whose
// prompt_file basename matches promptKey, for the ?format=json full-metadata
// case.
const fullRecordLine = `{"v":1,"run":"run-20261012-120000.log","cli":"qwen","worker":"sysdev-coder","runner":"sysdev-lead","status":"ok","exit_code":0,"timeout_secs":3600,"started_at":"2026-10-12T11:59:58Z","ended_at":"2026-10-12T12:00:00Z","log_bytes":42,"prompt_file":"/root/agentteams-fs/shared/tasks/t1/coding-prompts/20261012-120000.txt","prompt_sha256":"abc123sha","prompt_summary":"do the thing"}`

func TestListCodingRuns_EnrichedFromRunsJSONL(t *testing.T) {
	store := ossfake.NewMemory()
	putRun(store, teamRunKey, "alpha t1 run")
	// A second run in the same task with no runs.jsonl record — its degraded
	// fields are asserted alongside the enriched one.
	putRun(store, "teams/alpha-team/shared/tasks/t1/workspace/coding-cli-logs/run-20261012-130000.log", "alpha t1 second run")
	putRun(store, runsJSONLKey, fullRecordLine)
	h := newCodingRunsTestHandler(t, store, team("alpha-team"))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/coding-runs", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.ListCodingRuns(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	var runs []codingRun
	if err := json.Unmarshal(rec.Body.Bytes(), &runs); err != nil {
		t.Fatalf("decode list: %v", err)
	}
	if len(runs) != 2 {
		t.Fatalf("runs = %d, want 2: %+v", len(runs), runs)
	}
	// Files list in name order: the recorded run first.
	enriched := runs[0]
	if enriched.Run != "run-20261012-120000.log" {
		t.Fatalf("runs[0] = %q, want the recorded run first: %+v", enriched.Run, runs)
	}
	if enriched.Cli != "qwen" || enriched.Worker != "sysdev-coder" || enriched.Status != "ok" {
		t.Fatalf("enriched metadata = %+v, want cli=qwen worker=sysdev-coder status=ok", enriched)
	}
	if enriched.ExitCode == nil || *enriched.ExitCode != 0 {
		t.Fatalf("exit_code = %v, want 0 shown (0 is a legal value, not 'unknown')", enriched.ExitCode)
	}
	if enriched.StartedAt != "2026-10-12T11:59:58Z" {
		t.Fatalf("started_at = %q, want the record's value (not the filename fallback)", enriched.StartedAt)
	}
	if enriched.EndedAt != "2026-10-12T12:00:00Z" || enriched.LogBytes == nil || *enriched.LogBytes != 42 {
		t.Fatalf("ended_at/log_bytes = %q/%v, want the record's values", enriched.EndedAt, enriched.LogBytes)
	}
	// The run without a record degrades to base fields + filename fallback.
	fallback := runs[1]
	if fallback.Run != "run-20261012-130000.log" {
		t.Fatalf("runs[1] = %+v, want the unrecorded run", fallback)
	}
	if fallback.Cli != "" || fallback.Worker != "" || fallback.Status != "" ||
		fallback.ExitCode != nil || fallback.EndedAt != "" || fallback.LogBytes != nil {
		t.Fatalf("fallback run must not carry record metadata: %+v", fallback)
	}
	if fallback.StartedAt != "2026-10-12T13:00:00Z" {
		t.Fatalf("fallback started_at = %q, want filename-derived 2026-10-12T13:00:00Z", fallback.StartedAt)
	}
}

func TestListCodingRuns_MissingRunsJSONLFallsBackToFilename(t *testing.T) {
	// No runs.jsonl at all (an older wrapper version): base fields plus the
	// filename-derived started_at, and nothing else.
	store := ossfake.NewMemory()
	putRun(store, teamRunKey, "alpha t1 run")
	h := newCodingRunsTestHandler(t, store, team("alpha-team"))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/coding-runs", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.ListCodingRuns(rec, req)

	var runs []codingRun
	_ = json.Unmarshal(rec.Body.Bytes(), &runs)
	if len(runs) != 1 {
		t.Fatalf("runs = %d, want 1: %+v", len(runs), runs)
	}
	if runs[0].StartedAt != "2026-10-12T12:00:00Z" {
		t.Fatalf("started_at = %q, want filename-derived 2026-10-12T12:00:00Z", runs[0].StartedAt)
	}
	// Unknown metadata keys are omitted from the wire (omitempty contract).
	var rawList []map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &rawList); err != nil {
		t.Fatalf("decode raw list: %v", err)
	}
	for _, key := range []string{"cli", "worker", "status", "exit_code", "ended_at", "log_bytes"} {
		if _, ok := rawList[0][key]; ok {
			t.Fatalf("key %q must be omitted when unknown, got: %v", key, rawList[0])
		}
	}
}

func TestListCodingRuns_MalformedRunsJSONLIsTolerated(t *testing.T) {
	// Bad lines (garbage, a record without a run name, a blank line) are
	// skipped; the one good line is still applied.
	store := ossfake.NewMemory()
	putRun(store, teamRunKey, "alpha t1 run")
	putRun(store, runsJSONLKey, "not json at all\n{\"v\":1}\n\n"+`{"v":1,"run":"run-20261012-120000.log","cli":"qwen","status":"failed","exit_code":3}`+"\n")
	h := newCodingRunsTestHandler(t, store, team("alpha-team"))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/coding-runs", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.ListCodingRuns(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("malformed runs.jsonl must not fail the list: status = %d: %s", rec.Code, rec.Body.String())
	}
	var runs []codingRun
	_ = json.Unmarshal(rec.Body.Bytes(), &runs)
	if len(runs) != 1 {
		t.Fatalf("runs = %d, want 1: %+v", len(runs), runs)
	}
	if runs[0].Cli != "qwen" || runs[0].Status != "failed" || runs[0].ExitCode == nil || *runs[0].ExitCode != 3 {
		t.Fatalf("the good line must be applied, bad lines skipped: %+v", runs[0])
	}
}

func getRunJSON(h *CodingRunsHandler, taskID, run string, caller *authpkg.CallerIdentity) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, "/api/v1/coding-runs/"+taskID+"/runs/"+run+"?format=json", nil)
	req.SetPathValue("taskID", taskID)
	req.SetPathValue("run", run)
	if caller != nil {
		req = withCaller(req, caller)
	}
	rec := httptest.NewRecorder()
	h.GetCodingRun(rec, req)
	return rec
}

func TestGetCodingRun_JSONFormat_FullMetadata(t *testing.T) {
	store := ossfake.NewMemory()
	body := "[run-coding-cli] cli=qwen workspace=/root/agentteams-fs/shared/tasks/t1/workspace timeout=3600s\n<full transcript>"
	putRun(store, teamRunKey, body)
	putRun(store, runsJSONLKey, fullRecordLine)
	const promptBody = "fix the parser\nplease be careful"
	putRun(store, promptKey, promptBody)
	h := newCodingRunsTestHandler(t, store, team("alpha-team"))

	rec := getRunJSON(h, "t1", "run-20261012-120000.log", &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Fatalf("content-type = %q, want application/json", ct)
	}
	var d codingRunDetail
	if err := json.Unmarshal(rec.Body.Bytes(), &d); err != nil {
		t.Fatalf("decode detail: %v", err)
	}
	// Envelope.
	if d.TaskID != "t1" || d.Run != "run-20261012-120000.log" || d.Team != "alpha-team" {
		t.Fatalf("envelope = %+v, want task t1 / run / team alpha-team", d)
	}
	// Meta (from the runs.jsonl record, fields pass-through).
	if d.Meta.Cli != "qwen" || d.Meta.Worker != "sysdev-coder" || d.Meta.Runner != "sysdev-lead" ||
		d.Meta.Status != "ok" || d.Meta.ExitCode == nil || *d.Meta.ExitCode != 0 ||
		d.Meta.TimeoutSecs == nil || *d.Meta.TimeoutSecs != 3600 ||
		d.Meta.StartedAt != "2026-10-12T11:59:58Z" || d.Meta.EndedAt != "2026-10-12T12:00:00Z" ||
		d.Meta.LogBytes == nil || *d.Meta.LogBytes != 42 ||
		d.Meta.PromptFile != "/root/agentteams-fs/shared/tasks/t1/coding-prompts/20261012-120000.txt" ||
		d.Meta.PromptSHA256 != "abc123sha" {
		t.Fatalf("meta = %+v, want the record's fields pass-through", d.Meta)
	}
	// Input (prompt mirrored and available).
	if !d.Input.PromptAvailable || d.Input.Prompt != promptBody ||
		d.Input.PromptSummary != "do the thing" || d.Input.PromptSHA256 != "abc123sha" {
		t.Fatalf("input = %+v, want available prompt + summary + sha", d.Input)
	}
	// Output (transcript byte-transparent inside the envelope).
	if d.Output.Transcript != body || d.Output.Status != "ok" || d.Output.ExitCode == nil || *d.Output.ExitCode != 0 {
		t.Fatalf("output = %+v, want transcript + status + exit_code", d.Output)
	}
}

func TestGetCodingRun_JSONFormat_PromptUnavailable(t *testing.T) {
	// The record names a prompt the mirror never stored (a pre-fix run):
	// prompt_available=false, the prompt key omitted, summary/sha still
	// surface from the record.
	store := ossfake.NewMemory()
	putRun(store, teamRunKey, "transcript")
	putRun(store, runsJSONLKey, `{"v":1,"run":"run-20261012-120000.log","cli":"qwen","status":"failed","exit_code":1,"prompt_file":"/root/agentteams-fs/shared/tasks/t1/coding-prompts/20261012-120000.txt","prompt_sha256":"deadbeef","prompt_summary":"do the thing"}`)
	h := newCodingRunsTestHandler(t, store, team("alpha-team"))

	rec := getRunJSON(h, "t1", "run-20261012-120000.log", &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	var d codingRunDetail
	if err := json.Unmarshal(rec.Body.Bytes(), &d); err != nil {
		t.Fatalf("decode detail: %v", err)
	}
	if d.Input.PromptAvailable || d.Input.Prompt != "" {
		t.Fatalf("input = %+v, want prompt unavailable + empty", d.Input)
	}
	if d.Input.PromptSummary != "do the thing" || d.Input.PromptSHA256 != "deadbeef" {
		t.Fatalf("input = %+v, want summary/sha from the record even when the prompt is unavailable", d.Input)
	}
	if d.Output.Status != "failed" || d.Output.ExitCode == nil || *d.Output.ExitCode != 1 {
		t.Fatalf("output = %+v, want status=failed exit_code=1", d.Output)
	}
	// The prompt key is omitted from the wire (empty-value contract).
	var raw map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &raw); err != nil {
		t.Fatalf("decode raw detail: %v", err)
	}
	inputRaw, ok := raw["input"].(map[string]any)
	if !ok {
		t.Fatalf("input missing from raw detail: %v", raw)
	}
	if _, ok := inputRaw["prompt"]; ok {
		t.Fatalf("prompt key must be omitted when unavailable: %v", inputRaw)
	}
}

func TestGetCodingRun_JSONFormat_NoMetadata(t *testing.T) {
	// No runs.jsonl and no prompt object: meta degrades to an empty shell and
	// the transcript stays available — a metadata read failure never 500s.
	store := ossfake.NewMemory()
	body := "raw transcript only"
	putRun(store, teamRunKey, body)
	h := newCodingRunsTestHandler(t, store, team("alpha-team"))

	rec := getRunJSON(h, "t1", "run-20261012-120000.log", &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (degrade, not 500): %s", rec.Code, rec.Body.String())
	}
	var d codingRunDetail
	if err := json.Unmarshal(rec.Body.Bytes(), &d); err != nil {
		t.Fatalf("decode detail: %v", err)
	}
	var raw map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &raw); err != nil {
		t.Fatalf("decode raw detail: %v", err)
	}
	metaRaw, ok := raw["meta"].(map[string]any)
	if !ok {
		t.Fatalf("meta must be an object (empty shell), got: %v", raw["meta"])
	}
	if len(metaRaw) != 0 {
		t.Fatalf("meta = %v, want an empty shell when there is no record", metaRaw)
	}
	if d.Input.PromptAvailable || d.Input.Prompt != "" || d.Input.PromptSummary != "" || d.Input.PromptSHA256 != "" {
		t.Fatalf("input = %+v, want fully degraded", d.Input)
	}
	if d.Output.Transcript != body || d.Output.Status != "" || d.Output.ExitCode != nil {
		t.Fatalf("output = %+v, want the raw transcript with no metadata", d.Output)
	}
}

func TestGetCodingRun_JSONFormat_PromptBasenameFallback(t *testing.T) {
	// Record without prompt_file: the prompt basename is derived from the run
	// name (run-20261012-120000.log → 20261012-120000.txt) when the mirror
	// made it available.
	store := ossfake.NewMemory()
	putRun(store, teamRunKey, "transcript")
	putRun(store, runsJSONLKey, `{"v":1,"run":"run-20261012-120000.log","cli":"qwen","status":"ok","exit_code":0}`)
	const promptBody = "derived basename prompt"
	putRun(store, promptKey, promptBody)
	h := newCodingRunsTestHandler(t, store, team("alpha-team"))

	rec := getRunJSON(h, "t1", "run-20261012-120000.log", &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	var d codingRunDetail
	if err := json.Unmarshal(rec.Body.Bytes(), &d); err != nil {
		t.Fatalf("decode detail: %v", err)
	}
	if !d.Input.PromptAvailable || d.Input.Prompt != promptBody {
		t.Fatalf("input = %+v, want the prompt read via the run-name-derived basename", d.Input)
	}
}

func TestGetCodingRun_L2OwnTeam(t *testing.T) {
	store := ossfake.NewMemory()
	putRun(store, teamRunKey, "alpha t1")
	h := newCodingRunsTestHandler(t, store, team("alpha-team"))
	rec := getRun(h, "t1", "run-20261012-120000.log", &authpkg.CallerIdentity{Role: authpkg.RoleTeamLeader, Username: "alpha-lead", Team: "alpha-team"})
	if rec.Code != http.StatusOK {
		t.Fatalf("L2 own-team status = %d, want 200", rec.Code)
	}
}

func TestGetCodingRun_L2OtherTeam(t *testing.T) {
	store := ossfake.NewMemory()
	putRun(store, teamRunKey, "alpha t1")
	h := newCodingRunsTestHandler(t, store, team("alpha-team"))
	// beta leader reading an alpha-team run → 404 (W4, no probing).
	rec := getRun(h, "t1", "run-20261012-120000.log", &authpkg.CallerIdentity{Role: authpkg.RoleTeamLeader, Username: "beta-lead", Team: "beta-team"})
	if rec.Code != http.StatusNotFound {
		t.Fatalf("L2 cross-team status = %d, want 404", rec.Code)
	}
}

func TestGetCodingRun_L2CannotSeeStandalone(t *testing.T) {
	store := ossfake.NewMemory()
	putRun(store, standRunKey, "standalone t9")
	h := newCodingRunsTestHandler(t, store, team("alpha-team"))
	// L2 leader cannot read a standalone (global shared/) run — L1-only.
	rec := getRun(h, "t9", "run-20261012-120100.log", &authpkg.CallerIdentity{Role: authpkg.RoleTeamLeader, Username: "alpha-lead", Team: "alpha-team"})
	if rec.Code != http.StatusNotFound {
		t.Fatalf("L2 standalone status = %d, want 404", rec.Code)
	}
	// But admin can.
	rec2 := getRun(h, "t9", "run-20261012-120100.log", &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	if rec2.Code != http.StatusOK {
		t.Fatalf("admin standalone status = %d, want 200", rec2.Code)
	}
}

func TestGetCodingRun_InvalidTaskID(t *testing.T) {
	store := ossfake.NewMemory()
	h := newCodingRunsTestHandler(t, store)
	rec := getRun(h, "../../etc", "run-20261012-120000.log", &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("traversal taskID status = %d, want 400", rec.Code)
	}
}

func TestGetCodingRun_InvalidRun(t *testing.T) {
	store := ossfake.NewMemory()
	putRun(store, teamRunKey, "x")
	h := newCodingRunsTestHandler(t, store, team("alpha-team"))
	rec := getRun(h, "t1", "run-evil.log", &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("bad run status = %d, want 400", rec.Code)
	}
}

func TestGetCodingRun_NotFound(t *testing.T) {
	store := ossfake.NewMemory()
	// task t1 exists (has a dir marker) but no matching run.
	putRun(store, teamRunKey, "a run")
	h := newCodingRunsTestHandler(t, store, team("alpha-team"))
	rec := getRun(h, "t1", "run-20260101-000000.log", &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	if rec.Code != http.StatusNotFound {
		t.Fatalf("missing run status = %d, want 404", rec.Code)
	}
}

// TestListCodingRuns_CapIsGlobalAcrossTeams pins the list cap as GLOBAL across
// the caller's visible prefixes, not per-prefix. Regression for the case the
// upstream maintainer reproduced: two teams with codingRunsListCap runs each
// returned 201 because the inner file/task loops broke at the cap but the
// outer visible-prefix loop kept going into the next team's prefix. The break
// must stop the outer traversal as well.
func TestListCodingRuns_CapIsGlobalAcrossTeams(t *testing.T) {
	store := ossfake.NewMemory()
	// Two teams, each with exactly codingRunsListCap runs (one per task).
	for i := 0; i < codingRunsListCap; i++ {
		putRun(store, fmt.Sprintf("teams/alpha-team/shared/tasks/a%03d/workspace/coding-cli-logs/run-20261012-120000.log", i), "alpha")
		putRun(store, fmt.Sprintf("teams/beta-team/shared/tasks/b%03d/workspace/coding-cli-logs/run-20261012-120000.log", i), "beta")
	}
	h := newCodingRunsTestHandler(t, store, team("alpha-team"), team("beta-team"))
	req := httptest.NewRequest(http.MethodGet, "/api/v1/coding-runs", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.ListCodingRuns(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", rec.Code, rec.Body.String())
	}
	var runs []codingRun
	if err := json.Unmarshal(rec.Body.Bytes(), &runs); err != nil {
		t.Fatalf("decode list: %v", err)
	}
	if len(runs) != codingRunsListCap {
		t.Fatalf("len(runs) = %d, want global cap %d (outer prefix loop must stop once the cap is reached)", len(runs), codingRunsListCap)
	}
}
