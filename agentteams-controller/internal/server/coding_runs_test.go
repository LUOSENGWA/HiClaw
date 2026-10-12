package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
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
