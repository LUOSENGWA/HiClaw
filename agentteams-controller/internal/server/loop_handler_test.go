package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	v1beta1 "github.com/agentscope-ai/AgentTeams/agentteams-controller/api/v1beta1"
	authpkg "github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/auth"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func newLoopTestScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := v1beta1.AddToScheme(scheme); err != nil {
		t.Fatalf("add v1beta1 scheme: %v", err)
	}
	return scheme
}

func newLoopTestHandler(t *testing.T, objects ...client.Object) *LoopHandler {
	t.Helper()
	objs := make([]runtime.Object, 0, len(objects))
	for _, o := range objects {
		objs = append(objs, o)
	}
	c := fake.NewClientBuilder().WithScheme(newLoopTestScheme(t)).WithRuntimeObjects(objs...).Build()
	return &LoopHandler{Client: c, Namespace: "default"}
}

func testLoop(name, teamName string, paused bool, annotations map[string]string) *v1beta1.Loop {
	return &v1beta1.Loop{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "default", Annotations: annotations},
		Spec:       v1beta1.LoopSpec{TeamName: teamName, Paused: paused},
	}
}

func loopRequest(t *testing.T, id, body string, caller *authpkg.CallerIdentity, handler http.HandlerFunc) *httptest.ResponseRecorder {
	t.Helper()
	var req *http.Request
	if body != "" {
		req = httptest.NewRequest(http.MethodPost, "/api/v1/loops/"+id+"/pause", strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
	} else {
		req = httptest.NewRequest(http.MethodPost, "/api/v1/loops/"+id+"/pause", nil)
	}
	req.SetPathValue("id", id)
	if caller != nil {
		req = withCaller(req, caller)
	}
	rec := httptest.NewRecorder()
	handler(rec, req)
	return rec
}

func getLoop(t *testing.T, h *LoopHandler, id string) *v1beta1.Loop {
	t.Helper()
	loop := &v1beta1.Loop{}
	if err := h.Client.Get(context.Background(), client.ObjectKey{Namespace: "default", Name: id}, loop); err != nil {
		t.Fatalf("get loop: %v", err)
	}
	return loop
}

func TestPauseLoop_SuccessSetsSpecAndAnnotation(t *testing.T) {
	h := newLoopTestHandler(t, testLoop("loop-1", "alpha-team", false, nil))

	// Explicit source.
	rec := loopRequest(t, "loop-1", `{"source":"project"}`,
		&authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"}, h.PauseLoop)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	loop := getLoop(t, h, "loop-1")
	if !loop.Spec.Paused {
		t.Fatal("spec.paused not set")
	}
	if got := loop.Annotations[annotationLoopPausedBy]; got != "project" {
		t.Fatalf("annotation=%q want project", got)
	}

	// Default source (no body) overwrites via last-writer-wins.
	rec = loopRequest(t, "loop-1", "",
		&authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"}, h.PauseLoop)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	loop = getLoop(t, h, "loop-1")
	if got := loop.Annotations[annotationLoopPausedBy]; got != "user" {
		t.Fatalf("annotation=%q want user (last-writer-wins)", got)
	}

	// Invalid source -> 400.
	rec = loopRequest(t, "loop-1", `{"source":"bogus"}`,
		&authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"}, h.PauseLoop)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status=%d want 400 body=%s", rec.Code, rec.Body.String())
	}
}

func TestPauseLoop_TeamScopeAndWorkerDeny(t *testing.T) {
	// Worker is denied even for pause (execution side has no scheduling
	// veto).
	h := newLoopTestHandler(t, testLoop("loop-1", "alpha-team", false, nil))
	rec := loopRequest(t, "loop-1", "",
		&authpkg.CallerIdentity{Role: authpkg.RoleWorker, Username: "w1", Team: "alpha-team"}, h.PauseLoop)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("worker pause status=%d want 403 body=%s", rec.Code, rec.Body.String())
	}

	// Team leader of another team is denied (handler-side team boundary).
	rec = loopRequest(t, "loop-1", "",
		&authpkg.CallerIdentity{Role: authpkg.RoleTeamLeader, Username: "leader-b", Team: "beta-team"}, h.PauseLoop)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("cross-team leader pause status=%d want 403 body=%s", rec.Code, rec.Body.String())
	}

	// Same-team leader and same-team human succeed.
	rec = loopRequest(t, "loop-1", "",
		&authpkg.CallerIdentity{Role: authpkg.RoleTeamLeader, Username: "leader-a", Team: "alpha-team"}, h.PauseLoop)
	if rec.Code != http.StatusOK {
		t.Fatalf("same-team leader pause status=%d want 200 body=%s", rec.Code, rec.Body.String())
	}
	if !getLoop(t, h, "loop-1").Spec.Paused {
		t.Fatal("same-team leader pause did not set spec.paused")
	}

	rec = loopRequest(t, "loop-1", `{"source":"project"}`,
		&authpkg.CallerIdentity{Role: authpkg.RoleHuman, Username: "human-a", Teams: []string{"alpha-team"}}, h.PauseLoop)
	if rec.Code != http.StatusOK {
		t.Fatalf("same-team human pause status=%d want 200 body=%s", rec.Code, rec.Body.String())
	}

	// Human of another team is denied.
	h2 := newLoopTestHandler(t, testLoop("loop-1", "alpha-team", false, nil))
	rec = loopRequest(t, "loop-1", "",
		&authpkg.CallerIdentity{Role: authpkg.RoleHuman, Username: "human-b", Teams: []string{"beta-team"}}, h2.PauseLoop)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("cross-team human pause status=%d want 403 body=%s", rec.Code, rec.Body.String())
	}
}

func TestResumeLoop_SuccessClearsSpecAndAnnotation(t *testing.T) {
	h := newLoopTestHandler(t, testLoop("loop-1", "alpha-team", true,
		map[string]string{annotationLoopPausedBy: "project"}))

	rec := loopRequest(t, "loop-1", "",
		&authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"}, h.ResumeLoop)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	loop := getLoop(t, h, "loop-1")
	if loop.Spec.Paused {
		t.Fatal("spec.paused still true after resume")
	}
	if _, ok := loop.Annotations[annotationLoopPausedBy]; ok {
		t.Fatal("paused-by annotation not cleared")
	}

	// Paused with an empty annotation (e.g. pre-INT-7b pause) is resumable.
	h2 := newLoopTestHandler(t, testLoop("loop-2", "alpha-team", true, nil))
	rec = loopRequest(t, "loop-2", "",
		&authpkg.CallerIdentity{Role: authpkg.RoleHuman, Username: "human-a", Teams: []string{"alpha-team"}}, h2.ResumeLoop)
	if rec.Code != http.StatusOK {
		t.Fatalf("empty-annotation resume status=%d want 200 body=%s", rec.Code, rec.Body.String())
	}
	if getLoop(t, h2, "loop-2").Spec.Paused {
		t.Fatal("loop-2 still paused after resume")
	}
}

func TestResumeLoop_NotPausedIsNoOp(t *testing.T) {
	h := newLoopTestHandler(t, testLoop("loop-1", "alpha-team", false, nil))

	rec := loopRequest(t, "loop-1", "",
		&authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"}, h.ResumeLoop)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var resp map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp["resumed"] != false {
		t.Fatalf("resumed=%v want false (no-op)", resp["resumed"])
	}
}

func TestResumeLoop_UserSourceConflicts(t *testing.T) {
	h := newLoopTestHandler(t, testLoop("loop-1", "alpha-team", true,
		map[string]string{annotationLoopPausedBy: "user"}))

	rec := loopRequest(t, "loop-1", "",
		&authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"}, h.ResumeLoop)
	if rec.Code != http.StatusConflict {
		t.Fatalf("status=%d want 409 body=%s", rec.Code, rec.Body.String())
	}
	var resp struct {
		CurrentSource string `json:"current_source"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.CurrentSource != "user" {
		t.Fatalf("current_source=%q want user", resp.CurrentSource)
	}
	if !getLoop(t, h, "loop-1").Spec.Paused {
		t.Fatal("loop unpaused despite 409 conflict")
	}
}

func TestResumeLoop_AgentRolesDenied(t *testing.T) {
	// Manager, team leader, and worker are all denied on resume
	// (INT-7b ruling: admin + L2 human only — check-the-balance).
	mk := func() *LoopHandler {
		return newLoopTestHandler(t, testLoop("loop-1", "alpha-team", true,
			map[string]string{annotationLoopPausedBy: "user"}))
	}

	cases := []struct {
		name   string
		caller *authpkg.CallerIdentity
	}{
		{"manager", &authpkg.CallerIdentity{Role: authpkg.RoleManager, Username: "manager"}},
		{"team-leader", &authpkg.CallerIdentity{Role: authpkg.RoleTeamLeader, Username: "leader-a", Team: "alpha-team"}},
		{"worker", &authpkg.CallerIdentity{Role: authpkg.RoleWorker, Username: "w1", Team: "alpha-team"}},
	}
	for _, tc := range cases {
		h := mk()
		rec := loopRequest(t, "loop-1", "", tc.caller, h.ResumeLoop)
		if rec.Code != http.StatusForbidden {
			t.Fatalf("%s resume status=%d want 403 body=%s", tc.name, rec.Code, rec.Body.String())
		}
		if !getLoop(t, h, "loop-1").Spec.Paused {
			t.Fatalf("%s resume mutated the loop despite 403", tc.name)
		}
	}
}

func TestPauseResumeLoop_MissingLoop404(t *testing.T) {
	h := newLoopTestHandler(t)

	rec := loopRequest(t, "ghost", "",
		&authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"}, h.PauseLoop)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("pause missing status=%d want 404 body=%s", rec.Code, rec.Body.String())
	}
	rec = loopRequest(t, "ghost", "",
		&authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"}, h.ResumeLoop)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("resume missing status=%d want 404 body=%s", rec.Code, rec.Body.String())
	}
}
