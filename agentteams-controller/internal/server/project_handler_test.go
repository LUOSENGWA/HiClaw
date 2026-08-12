package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"

	v1beta1 "github.com/agentscope-ai/AgentTeams/agentteams-controller/api/v1beta1"
	authpkg "github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/auth"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/oss"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/oss/ossfake"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func newProjectTestScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := v1beta1.AddToScheme(scheme); err != nil {
		t.Fatalf("add v1beta1 scheme: %v", err)
	}
	return scheme
}

// mcLikeOSS mimics the real MinIOClient.ListObjects semantics (mc ls returns
// direct child names, e.g. "p1/" for a project directory) on top of the
// in-memory fake, which itself returns full object keys.
type mcLikeOSS struct {
	*ossfake.Memory
	listCalls int
	failList  bool
	failGet   bool
}

func (m *mcLikeOSS) ListObjects(_ context.Context, prefix string) ([]string, error) {
	m.listCalls++
	if m.failList {
		return nil, errors.New("oss list failed")
	}
	keys, err := m.Memory.ListObjects(context.Background(), prefix)
	if err != nil {
		return nil, err
	}
	seen := map[string]bool{}
	out := make([]string, 0)
	for _, k := range keys {
		rest := strings.TrimPrefix(k, prefix)
		parts := strings.SplitN(rest, "/", 2)
		if len(parts) != 2 {
			continue
		}
		dir := parts[0] + "/"
		if !seen[dir] {
			seen[dir] = true
			out = append(out, dir)
		}
	}
	sort.Strings(out)
	return out, nil
}

func (m *mcLikeOSS) GetObject(ctx context.Context, key string) ([]byte, error) {
	if m.failGet {
		return nil, errors.New("oss get failed")
	}
	return m.Memory.GetObject(ctx, key)
}

// newProjectTestHandler builds a ProjectHandler with an in-memory OSS store and
// a fake K8s client containing the given Teams.
func newProjectTestHandler(t *testing.T, store *ossfake.Memory, teams ...*v1beta1.Team) *ProjectHandler {
	t.Helper()
	objs := make([]runtime.Object, 0, len(teams))
	for _, tm := range teams {
		objs = append(objs, tm)
	}
	k8s := fake.NewClientBuilder().WithScheme(newProjectTestScheme(t)).WithRuntimeObjects(objs...).Build()
	var o oss.StorageClient = &mcLikeOSS{Memory: store}
	return NewProjectHandler(k8s, "default", o)
}

func withCaller(req *http.Request, c *authpkg.CallerIdentity) *http.Request {
	if c == nil {
		return req
	}
	return req.WithContext(context.WithValue(req.Context(), authpkg.CallerKeyForTest(), c))
}

func putProject(store *ossfake.Memory, key string, meta map[string]any) {
	data, _ := json.Marshal(meta)
	_ = store.PutObject(context.Background(), key, data)
}

func team(name string) *v1beta1.Team {
	return &v1beta1.Team{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "default"},
		Spec:       v1beta1.TeamSpec{TeamName: name},
	}
}

func TestListProjects_AdminScansAllPrefixes(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "teams/alpha-team/shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1", "title": "Alpha Project", "status": "active", "plan_type": "dag", "team_id": "alpha-team",
	})
	putProject(store, "shared/projects/p2/meta.json", map[string]any{
		"project_id": "p2", "title": "Standalone", "status": "active", "plan_type": "loop",
	})
	h := newProjectTestHandler(t, store, team("alpha-team"))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.ListProjects(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var resp struct {
		Projects []map[string]any `json:"projects"`
		Total    int              `json:"total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Total != 2 {
		t.Fatalf("total=%d, want 2 (team + standalone)", resp.Total)
	}
}

func TestListProjects_TeamLeaderSeesOwnTeamOnly(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "teams/alpha-team/shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1", "title": "Alpha Project", "status": "active", "plan_type": "dag", "team_id": "alpha-team",
	})
	putProject(store, "teams/beta-team/shared/projects/p2/meta.json", map[string]any{
		"project_id": "p2", "title": "Beta Project", "status": "active", "plan_type": "dag", "team_id": "beta-team",
	})
	putProject(store, "shared/projects/p3/meta.json", map[string]any{
		"project_id": "p3", "title": "Standalone", "status": "active", "plan_type": "dag",
	})
	h := newProjectTestHandler(t, store, team("alpha-team"), team("beta-team"))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleTeamLeader, Username: "alpha-lead", Team: "alpha-team"})
	rec := httptest.NewRecorder()
	h.ListProjects(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var resp struct {
		Projects []map[string]any `json:"projects"`
		Total    int              `json:"total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	ids := map[string]bool{}
	for _, p := range resp.Projects {
		ids[p["project_id"].(string)] = true
	}
	if !ids["p1"] {
		t.Fatalf("team-leader should see own team project, got %v", ids)
	}
	if ids["p2"] || ids["p3"] {
		t.Fatalf("team-leader should NOT see beta or standalone projects, got %v", ids)
	}
}

func TestListProjects_EmptyStoreReturnsEmpty(t *testing.T) {
	h := newProjectTestHandler(t, ossfake.NewMemory())
	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleManager, Username: "manager"})
	rec := httptest.NewRecorder()
	h.ListProjects(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s, want 200 with empty list", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"projects":[]`) {
		t.Fatalf("expected empty projects list, got %s", rec.Body.String())
	}
}

func TestGetProjectWorkflow_DagNormalization(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "teams/alpha-team/shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1",
		"title":      "Alpha Project",
		"status":     "active",
		"plan_type":  "dag",
		"team_id":    "alpha-team",
		"tasks": []map[string]any{
			{"task_id": "t1", "title": "Task 1", "assigned_to": "@w1", "depends_on": []string{}, "status": "completed"},
			{"task_id": "t2", "title": "Task 2", "assigned_to": "@w2", "depends_on": []string{"t1"}, "status": "assigned"},
			{"task_id": "t3", "title": "Task 3", "assigned_to": "@w3", "depends_on": []string{"t2"}, "status": "planned"},
		},
	})
	h := newProjectTestHandler(t, store, team("alpha-team"))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/workflow", nil)
	req.SetPathValue("id", "p1")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var wf workflowResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &wf); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(wf.Nodes) != 3 {
		t.Fatalf("nodes=%d, want 3", len(wf.Nodes))
	}
	if len(wf.Edges) != 2 {
		t.Fatalf("edges=%d, want 2 (t1→t2, t2→t3)", len(wf.Edges))
	}
	// t2 is ready (dep t1 completed); t3 waits on t2.
	if len(wf.Next) != 1 || wf.Next[0] != "t2" {
		t.Fatalf("next=%v, want [t2]", wf.Next)
	}
	// status normalization: assigned → delegated, planned → pending
	statuses := map[string]string{}
	for _, n := range wf.Nodes {
		statuses[n.ID] = n.Status
	}
	if statuses["t1"] != "completed" || statuses["t2"] != "delegated" || statuses["t3"] != "pending" {
		t.Fatalf("status normalization wrong: %v", statuses)
	}
	// values summary (StateSnapshot analog)
	if wf.Values == nil {
		t.Fatal("values summary missing")
	}
	if wf.Values.TaskCount["completed"] != 1 || wf.Values.TaskCount["delegated"] != 1 || wf.Values.TaskCount["pending"] != 1 {
		t.Fatalf("values.task_count wrong: %+v", wf.Values.TaskCount)
	}
	if wf.Values.Status != "active" || wf.Values.PlanType != "dag" {
		t.Fatalf("values project fields wrong: %+v", wf.Values)
	}
}

func TestGetProjectWorkflow_LoopReadyFromLoopTasks(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1",
		"title":      "Loop Project",
		"status":     "active",
		"plan_type":  "loop",
		"tasks":      []map[string]any{}, // loop projects keep tasks empty
		"loop": map[string]any{
			"goal": "g", "stop_condition": "s", "current_iteration": 1, "max_iterations": 5, "status": "running",
			"tasks": []map[string]any{
				{"task_id": "l1", "title": "Loop Step 1", "assigned_to": "@w1", "depends_on": []string{}, "status": "completed"},
				{"task_id": "l2", "title": "Loop Step 2", "assigned_to": "@w2", "depends_on": []string{"l1"}, "status": "assigned"},
			},
		},
	})
	h := newProjectTestHandler(t, store)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/workflow", nil)
	req.SetPathValue("id", "p1")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)

	var wf workflowResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &wf); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(wf.Nodes) != 2 {
		t.Fatalf("nodes=%d, want 2 (from loop.tasks)", len(wf.Nodes))
	}
	if len(wf.Next) != 1 || wf.Next[0] != "l2" {
		t.Fatalf("next=%v, want [l2] (loop ready semantics)", wf.Next)
	}
}

func TestGetProjectWorkflow_TaskLifecycleStatusNormalization(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1",
		"title":      "P",
		"status":     "active",
		"plan_type":  "dag",
		"tasks": []map[string]any{
			{"task_id": "t1", "title": "T1", "assigned_to": "@w1", "depends_on": []string{}, "status": "in_progress"},
			{"task_id": "t2", "title": "T2", "assigned_to": "@w2", "depends_on": []string{"t1"}, "status": "submitted"},
			{"task_id": "t3", "title": "T3", "assigned_to": "@w3", "depends_on": []string{"t2"}, "status": "planned"},
		},
	})
	h := newProjectTestHandler(t, store)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/workflow", nil)
	req.SetPathValue("id", "p1")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)

	var wf workflowResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &wf); err != nil {
		t.Fatalf("decode: %v", err)
	}
	statuses := map[string]string{}
	for _, n := range wf.Nodes {
		statuses[n.ID] = n.Status
	}
	if statuses["t1"] != "in-progress" || statuses["t2"] != "in-progress" {
		t.Fatalf("in_progress/submitted should normalize to in-progress, got %v", statuses)
	}
	// t3 is ready (deps t1/t2 not completed yet? no — t1/t2 are not completed,
	// so t3 is NOT ready). t2 submitted is not ready either.
	if len(wf.Next) != 0 {
		t.Fatalf("next=%v, want [] (no completed dependencies)", wf.Next)
	}
}

func TestListProjects_EffectiveTeamNameMapping(t *testing.T) {
	store := ossfake.NewMemory()
	// Team CR name "alpha-cr" with spec.teamName "alpha-team": storage is
	// supposed to live under teams/alpha-team/ (EffectiveTeamName wins).
	putProject(store, "teams/alpha-team/shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1", "title": "Alpha Project", "status": "active", "plan_type": "dag", "team_id": "alpha-team",
	})
	// Decoy under the CR-name prefix simulates a worker that synced using the
	// Worker API team field (resp.Team = CR name). Both prefixes are scanned
	// to tolerate the mismatch; TeamLeader scoping still uses effective name.
	putProject(store, "teams/alpha-cr/shared/projects/decoy/meta.json", map[string]any{
		"project_id": "decoy", "title": "Decoy", "status": "active", "plan_type": "dag",
	})
	alphaCR := &v1beta1.Team{
		ObjectMeta: metav1.ObjectMeta{Name: "alpha-cr", Namespace: "default"},
		Spec:       v1beta1.TeamSpec{TeamName: "alpha-team"},
	}
	h := newProjectTestHandler(t, store, alphaCR)

	// Admin sees both prefixes (tolerance scan).
	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.ListProjects(rec, req)

	var resp struct {
		Projects []map[string]any `json:"projects"`
		Total    int              `json:"total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Total != 2 {
		t.Fatalf("admin should see both prefixes (effective + CR name), got total=%d %+v", resp.Total, resp.Projects)
	}

	// TeamLeader (CR name alpha-cr) is scoped to the effective-name prefix only.
	req2 := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	req2 = withCaller(req2, &authpkg.CallerIdentity{Role: authpkg.RoleTeamLeader, Username: "alpha-lead", Team: "alpha-cr"})
	rec2 := httptest.NewRecorder()
	h.ListProjects(rec2, req2)

	var resp2 struct {
		Projects []map[string]any `json:"projects"`
		Total    int              `json:"total"`
	}
	if err := json.Unmarshal(rec2.Body.Bytes(), &resp2); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp2.Total != 1 || resp2.Projects[0]["project_id"] != "p1" {
		t.Fatalf("team-leader should see effective-name project only, got total=%d %+v", resp2.Total, resp2.Projects)
	}
}

func TestListProjects_MissingMetaSkipped(t *testing.T) {
	store := ossfake.NewMemory()
	// p1 has meta.json; p2 is a project dir without meta.json yet.
	putProject(store, "shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1", "title": "P1", "status": "active", "plan_type": "dag",
	})
	_ = store.PutObject(context.Background(), "shared/projects/p2/.agentteams-keep", []byte(""))
	h := newProjectTestHandler(t, store)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.ListProjects(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s, want 200 (missing meta skipped)", rec.Code, rec.Body.String())
	}
	var resp struct {
		Projects []map[string]any `json:"projects"`
		Total    int              `json:"total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Total != 1 || resp.Projects[0]["project_id"] != "p1" {
		t.Fatalf("want only p1 (p2 missing meta skipped), got total=%d %+v", resp.Total, resp.Projects)
	}
}

func TestHTTPServer_RegistersProjectRoutes(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1", "title": "P1", "status": "active", "plan_type": "dag",
	})
	k8s := fake.NewClientBuilder().WithScheme(newProjectTestScheme(t)).Build()
	var ossStore oss.StorageClient = &mcLikeOSS{Memory: store}
	srv := NewHTTPServer(":0", ServerDeps{
		Client:    k8s,
		Namespace: "default",
		OSS:       ossStore,
		AuthMw:    authpkg.NewMiddleware(nil, nil, nil, nil, ""),
	})

	// GET /api/v1/projects
	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	rec := httptest.NewRecorder()
	srv.Mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /api/v1/projects status=%d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"project_id":"p1"`) {
		t.Fatalf("expected p1 in list, got %s", rec.Body.String())
	}

	// GET /api/v1/projects/p1/workflow
	req2 := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/workflow", nil)
	rec2 := httptest.NewRecorder()
	srv.Mux.ServeHTTP(rec2, req2)
	if rec2.Code != http.StatusOK {
		t.Fatalf("GET /api/v1/projects/p1/workflow status=%d body=%s", rec2.Code, rec2.Body.String())
	}
	if !strings.Contains(rec2.Body.String(), `"project_id":"p1"`) {
		t.Fatalf("expected p1 in workflow, got %s", rec2.Body.String())
	}

	// Unmatched route should 404 (no wildcard shadowing)
	req3 := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/nope", nil)
	rec3 := httptest.NewRecorder()
	srv.Mux.ServeHTTP(rec3, req3)
	if rec3.Code == http.StatusOK {
		t.Fatalf("unmatched sub-route should not 200, got %d", rec3.Code)
	}
}

func TestGetProjectWorkflow_CorruptMetaNotFound(t *testing.T) {
	store := ossfake.NewMemory()
	_ = store.PutObject(context.Background(), "shared/projects/p1/meta.json", []byte("{truncated"))
	h := newProjectTestHandler(t, store)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/workflow", nil)
	req.SetPathValue("id", "p1")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status=%d body=%s, want 404 for corrupt meta (not 500)", rec.Code, rec.Body.String())
	}
}

func TestListProjects_SortedAndTeamFiltered(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "teams/alpha-team/shared/projects/p2/meta.json", map[string]any{
		"project_id": "p2", "title": "P2", "status": "active", "plan_type": "dag", "team_id": "alpha-team",
	})
	putProject(store, "shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1", "title": "P1", "status": "active", "plan_type": "dag",
	})
	putProject(store, "teams/beta-team/shared/projects/p3/meta.json", map[string]any{
		"project_id": "p3", "title": "P3", "status": "active", "plan_type": "dag", "team_id": "beta-team",
	})
	h := newProjectTestHandler(t, store, team("alpha-team"), team("beta-team"))

	// Sorted by project_id for admin.
	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.ListProjects(rec, req)
	var resp struct {
		Projects []map[string]any `json:"projects"`
		Total    int              `json:"total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Total != 3 {
		t.Fatalf("total=%d, want 3", resp.Total)
	}
	ids := []string{}
	for _, p := range resp.Projects {
		ids = append(ids, p["project_id"].(string))
	}
	if ids[0] != "p1" || ids[1] != "p2" || ids[2] != "p3" {
		t.Fatalf("projects not sorted: %v", ids)
	}

	// ?team=alpha-team filters (admin).
	req2 := httptest.NewRequest(http.MethodGet, "/api/v1/projects?team=alpha-team", nil)
	req2 = withCaller(req2, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec2 := httptest.NewRecorder()
	h.ListProjects(rec2, req2)
	var resp2 struct {
		Projects []map[string]any `json:"projects"`
		Total    int              `json:"total"`
	}
	if err := json.Unmarshal(rec2.Body.Bytes(), &resp2); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp2.Total != 1 || resp2.Projects[0]["project_id"] != "p2" {
		t.Fatalf("team filter failed, got %+v", resp2.Projects)
	}
}

func TestGetProjectWorkflow_LoopWaitingUserHasNoNext(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1",
		"title":      "Loop Project",
		"status":     "active",
		"plan_type":  "loop",
		"tasks":      []map[string]any{},
		"loop": map[string]any{
			"goal": "g", "stop_condition": "s", "current_iteration": 1, "max_iterations": 5, "status": "waiting_user",
			"tasks": []map[string]any{
				{"task_id": "l1", "title": "Loop Step 1", "assigned_to": "@w1", "depends_on": []string{}, "status": "assigned"},
			},
		},
	})
	h := newProjectTestHandler(t, store)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/workflow", nil)
	req.SetPathValue("id", "p1")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)

	var wf workflowResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &wf); err != nil {
		t.Fatalf("decode: %v", err)
	}
	// waiting_user loop has no ready nodes even though l1 deps are empty
	// (mirrors _ready_loop_nodes: loop.status in {completed, blocked, waiting_user}).
	if len(wf.Next) != 0 {
		t.Fatalf("next=%v, want [] for waiting_user loop", wf.Next)
	}
	// interrupt should surface.
	found := false
	for _, in := range wf.Interrupts {
		if in.ID == "loop" {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected loop interrupt, got %+v", wf.Interrupts)
	}
}

func TestGetProjectWorkflow_BlockedCreatesInterrupt(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1",
		"title":      "P",
		"status":     "active",
		"plan_type":  "dag",
		"tasks": []map[string]any{
			{"task_id": "t1", "title": "T1", "assigned_to": "@w1", "depends_on": []string{}, "status": "blocked"},
		},
	})
	h := newProjectTestHandler(t, store)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/workflow", nil)
	req.SetPathValue("id", "p1")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)

	var wf workflowResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &wf); err != nil {
		t.Fatalf("decode: %v", err)
	}
	found := false
	for _, in := range wf.Interrupts {
		if in.ID == "t1" {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected interrupt for blocked t1, got %+v", wf.Interrupts)
	}
}

func TestGetProjectWorkflow_LoopWaitingUserCreatesInterrupt(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1",
		"title":      "P",
		"status":     "active",
		"plan_type":  "loop",
		"tasks":      []map[string]any{},
		"loop": map[string]any{
			"goal": "g", "stop_condition": "s", "current_iteration": 1, "max_iterations": 5, "status": "waiting_user",
		},
	})
	h := newProjectTestHandler(t, store)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/workflow", nil)
	req.SetPathValue("id", "p1")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleManager, Username: "manager"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)

	var wf workflowResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &wf); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if wf.Loop == nil || wf.Loop.Status != "waiting_user" {
		t.Fatalf("loop not passed through: %+v", wf.Loop)
	}
	found := false
	for _, in := range wf.Interrupts {
		if in.ID == "loop" {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected loop interrupt, got %+v", wf.Interrupts)
	}
}

func TestGetProjectWorkflow_NotFound(t *testing.T) {
	h := newProjectTestHandler(t, ossfake.NewMemory())
	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/nope/workflow", nil)
	req.SetPathValue("id", "nope")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status=%d, want 404", rec.Code)
	}
}

// TestGetProjectWorkflow_PausedInterrupt guards W1: a paused project surfaces
// as a human interrupt (LangGraph semantics) in addition to status=paused.
func TestGetProjectWorkflow_PausedInterrupt(t *testing.T) {	store := ossfake.NewMemory()
	putProject(store, "shared/projects/paused1/meta.json", map[string]any{
		"project_id": "paused1", "title": "Paused", "status": "paused", "plan_type": "dag", "tasks": []map[string]any{},
	})
	h := newProjectTestHandler(t, store)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/paused1/workflow", nil)
	req.SetPathValue("id", "paused1")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleManager, Username: "manager"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var wf workflowResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &wf); err != nil {
		t.Fatalf("decode: %v", err)
	}
	found := false
	for _, in := range wf.Interrupts {
		if in.ID == "project" && in.Value == "paused" {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected paused project interrupt, got %+v", wf.Interrupts)
	}
}

// TestListProjects_SkipGetObjectFailure guards W5: a per-object GetObject
// failure must be skipped (not 500 the whole list); infrastructure-level
// ListObjects failures still 500 (covered by TestListProjects_OSSErrorReturns500).
func TestListProjects_SkipGetObjectFailure(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "shared/projects/good1/meta.json", map[string]any{
		"project_id": "good1", "title": "Good", "status": "active", "plan_type": "dag",
	})
	putProject(store, "shared/projects/bad1/meta.json", map[string]any{
		"project_id": "bad1", "title": "Bad", "status": "active", "plan_type": "dag",
	})
	m := &mcLikeOSS{Memory: store, failGet: true}
	scheme := newProjectTestScheme(t)
	k8s := fake.NewClientBuilder().WithScheme(scheme).WithRuntimeObjects(team("alpha-team")).Build()
	h := NewProjectHandler(k8s, "default", m)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.ListProjects(rec, req)

	// W5: GetObject failures are skipped, so the list still succeeds and
	// simply contains no projects from the failing store.
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d, want 200 (per-object failure skipped)", rec.Code)
	}
	var resp struct {
		Projects []map[string]any `json:"projects"`
		Total    int              `json:"total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Total != 0 {
		t.Fatalf("total=%d, want 0 (all GetObject failed, skipped)", resp.Total)
	}
}

func TestGetProjectWorkflow_TeamLeaderCrossTeamDenied(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "teams/beta-team/shared/projects/p2/meta.json", map[string]any{
		"project_id": "p2", "title": "Beta", "status": "active", "plan_type": "dag", "team_id": "beta-team",
	})
	h := newProjectTestHandler(t, store, team("beta-team"))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p2/workflow", nil)
	req.SetPathValue("id", "p2")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleTeamLeader, Username: "alpha-lead", Team: "alpha-team"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status=%d, want 404 for cross-team access (W4: hide project existence)", rec.Code)
	}
}

func TestGetProjectWorkflow_TeamLeaderStandaloneDenied(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "shared/projects/p3/meta.json", map[string]any{
		"project_id": "p3", "title": "Standalone", "status": "active", "plan_type": "dag",
	})
	h := newProjectTestHandler(t, store, team("alpha-team"))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p3/workflow", nil)
	req.SetPathValue("id", "p3")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleTeamLeader, Username: "alpha-lead", Team: "alpha-team"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status=%d, want 404 for standalone project (W4: hide existence from team leader)", rec.Code)
	}
}

// countingClient wraps the fake client to count K8s List round-trips.
type countingClient struct {
	client.Client
	listCalls int
}

func (c *countingClient) List(ctx context.Context, list client.ObjectList, opts ...client.ListOption) error {
	c.listCalls++
	return c.Client.List(ctx, list, opts...)
}

// TestGetProjectWorkflow_SingleK8sList guards O1: a workflow request must pay
// exactly one K8s TeamList round-trip — shared between meta resolution and the
// team-leader access check — not two.
func TestGetProjectWorkflow_SingleK8sList(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "teams/alpha-team/shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1", "title": "P1", "status": "active", "plan_type": "dag", "team_id": "alpha-team",
	})
	scheme := newProjectTestScheme(t)
	k8s := fake.NewClientBuilder().WithScheme(scheme).WithRuntimeObjects(team("alpha-team")).Build()
	cc := &countingClient{Client: k8s}
	var o oss.StorageClient = &mcLikeOSS{Memory: store}
	h := NewProjectHandler(cc, "default", o)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/workflow", nil)
	req.SetPathValue("id", "p1")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d, want 200; body=%s", rec.Code, rec.Body.String())
	}
	if cc.listCalls != 1 {
		t.Fatalf("K8s List calls=%d, want 1 (single list shared between meta resolution and access check)", cc.listCalls)
	}
}

// TestListProjects_TeamFilterSkipsOtherPrefixes guards O2: a ?team= filter
// must skip non-matching prefixes before hitting OSS (ListObjects), not scan
// every prefix and filter afterwards.
func TestListProjects_TeamFilterSkipsOtherPrefixes(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "teams/alpha-team/shared/projects/p2/meta.json", map[string]any{
		"project_id": "p2", "title": "P2", "status": "active", "plan_type": "dag", "team_id": "alpha-team",
	})
	putProject(store, "teams/beta-team/shared/projects/p3/meta.json", map[string]any{
		"project_id": "p3", "title": "P3", "status": "active", "plan_type": "dag", "team_id": "beta-team",
	})
	putProject(store, "shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1", "title": "P1", "status": "active", "plan_type": "dag",
	})
	m := &mcLikeOSS{Memory: store}
	scheme := newProjectTestScheme(t)
	k8s := fake.NewClientBuilder().WithScheme(scheme).WithRuntimeObjects(team("alpha-team"), team("beta-team")).Build()
	h := NewProjectHandler(k8s, "default", m)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects?team=alpha-team", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.ListProjects(rec, req)

	var resp struct {
		Projects []map[string]any `json:"projects"`
		Total    int              `json:"total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Total != 1 || resp.Projects[0]["project_id"] != "p2" {
		t.Fatalf("team filter result wrong: %+v", resp.Projects)
	}
	// alpha-team prefix only — beta-team and shared prefixes must not be listed.
	if m.listCalls != 1 {
		t.Fatalf("ListObjects calls=%d, want 1 (only the alpha-team prefix scanned)", m.listCalls)
	}
}

// TestGetProjectWorkflow_NextOnlyPlannedAssigned guards O5: only tasks whose
// raw status is planned/assigned can appear in next — empty or unknown
// statuses must not (upstream _ready_nodes skips them), even when their
// dependencies are all completed.
func TestGetProjectWorkflow_NextOnlyPlannedAssigned(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1", "title": "P1", "status": "active", "plan_type": "dag",
		"tasks": []map[string]any{
			{"task_id": "t1", "title": "T1", "status": "planned", "depends_on": []string{}},
			{"task_id": "t2", "title": "T2", "status": "", "depends_on": []string{}},
			{"task_id": "t3", "title": "T3", "status": "weird", "depends_on": []string{}},
		},
	})
	h := newProjectTestHandler(t, store)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/workflow", nil)
	req.SetPathValue("id", "p1")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var wf workflowResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &wf); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(wf.Next) != 1 || wf.Next[0] != "t1" {
		t.Fatalf("next=%v, want [t1] only (empty/unknown statuses are not ready)", wf.Next)
	}
}

// TestGetProjectWorkflow_SourceField guards O8: the workflow response must
// expose the project source label (matrix/dingtalk/wechat...) that TeamHarness
// writes into meta.json — humans need to know which channel a project came from.
func TestGetProjectWorkflow_SourceField(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "teams/alpha-team/shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1", "title": "P1", "status": "active", "plan_type": "dag",
		"team_id": "alpha-team", "source": "dingtalk",
		"tasks": []map[string]any{
			{"task_id": "t1", "title": "T1", "status": "planned", "depends_on": []string{}},
		},
	})
	h := newProjectTestHandler(t, store, team("alpha-team"))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/workflow", nil)
	req.SetPathValue("id", "p1")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var wf workflowResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &wf); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if wf.Source != "dingtalk" {
		t.Fatalf("source=%q, want dingtalk", wf.Source)
	}
}

// TestListProjects_L2HumanAggregatesTeams guards the multi-tenant L2 path: a
// human with AccessibleTeams [alpha, beta] sees projects from BOTH teams in a
// single list (no per-team SA switching).
func TestListProjects_L2HumanAggregatesTeams(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "teams/alpha-team/shared/projects/pa/meta.json", map[string]any{
		"project_id": "pa", "title": "PA", "status": "active", "plan_type": "dag", "team_id": "alpha-team",
	})
	putProject(store, "teams/beta-team/shared/projects/pb/meta.json", map[string]any{
		"project_id": "pb", "title": "PB", "status": "active", "plan_type": "dag", "team_id": "beta-team",
	})
	putProject(store, "teams/gamma-team/shared/projects/pc/meta.json", map[string]any{
		"project_id": "pc", "title": "PC", "status": "active", "plan_type": "dag", "team_id": "gamma-team",
	})
	h := newProjectTestHandler(t, store, team("alpha-team"), team("beta-team"), team("gamma-team"))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	// L2 human with two accessible teams (Human CR accessibleTeams = CR names).
	req = withCaller(req, &authpkg.CallerIdentity{
		Role: authpkg.RoleHuman, Username: "maizong", Teams: []string{"alpha-team", "beta-team"},
	})
	rec := httptest.NewRecorder()
	h.ListProjects(rec, req)

	var resp struct {
		Projects []map[string]any `json:"projects"`
		Total    int              `json:"total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Total != 2 {
		t.Fatalf("total=%d, want 2 (alpha+beta only, gamma hidden); got %+v", resp.Total, resp.Projects)
	}
	ids := map[string]bool{}
	for _, p := range resp.Projects {
		ids[p["project_id"].(string)] = true
	}
	if !ids["pa"] || !ids["pb"] || ids["pc"] {
		t.Fatalf("projects=%v, want pa+pb, no pc", ids)
	}
}

// TestGetProjectWorkflow_L2HumanAnyAccessibleTeam guards the multi-tenant L2
// read path: an L2 human can read a workflow from any accessible team but is
// denied projects outside their accessible set.
func TestGetProjectWorkflow_L2HumanAnyAccessibleTeam(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "teams/alpha-team/shared/projects/pa/meta.json", map[string]any{
		"project_id": "pa", "title": "PA", "status": "active", "plan_type": "dag", "team_id": "alpha-team",
		"tasks": []map[string]any{{"task_id": "t1", "title": "T1", "status": "planned", "depends_on": []string{}}},
	})
	putProject(store, "teams/gamma-team/shared/projects/pc/meta.json", map[string]any{
		"project_id": "pc", "title": "PC", "status": "active", "plan_type": "dag", "team_id": "gamma-team",
		"tasks": []map[string]any{{"task_id": "t1", "title": "T1", "status": "planned", "depends_on": []string{}}},
	})
	h := newProjectTestHandler(t, store, team("alpha-team"), team("gamma-team"))
	l2 := &authpkg.CallerIdentity{
		Role: authpkg.RoleHuman, Username: "maizong", Teams: []string{"alpha-team"},
	}

	// Accessible team -> OK.
	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/pa/workflow", nil)
	req.SetPathValue("id", "pa")
	req = withCaller(req, l2)
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("accessible team status=%d, want 200; body=%s", rec.Code, rec.Body.String())
	}

	// Non-accessible team -> 404 (W4: hide project existence).
	req2 := httptest.NewRequest(http.MethodGet, "/api/v1/projects/pc/workflow", nil)
	req2.SetPathValue("id", "pc")
	req2 = withCaller(req2, l2)
	rec2 := httptest.NewRecorder()
	h.GetProjectWorkflow(rec2, req2)
	if rec2.Code != http.StatusNotFound {
		t.Fatalf("non-accessible team status=%d, want 404 (W4: hide existence)", rec2.Code)
	}
}

// TestListProjects_OSSErrorReturns500 guards the explicit-failure path: an
// object-store failure surfaces as 500 (never a silently truncated list).
func TestListProjects_OSSErrorReturns500(t *testing.T) {
	store := ossfake.NewMemory()
	m := &mcLikeOSS{Memory: store, failList: true}
	scheme := newProjectTestScheme(t)
	k8s := fake.NewClientBuilder().WithScheme(scheme).WithRuntimeObjects(team("alpha-team")).Build()
	h := NewProjectHandler(k8s, "default", m)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.ListProjects(rec, req)
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status=%d, want 500 on OSS failure", rec.Code)
	}
}

// TestGetProjectWorkflow_K8sErrorReturns500 guards the K8s failure path:
// TeamList resolution errors surface as 500, not a false 404.
func TestGetProjectWorkflow_K8sErrorReturns500(t *testing.T) {
	store := ossfake.NewMemory()
	scheme := newProjectTestScheme(t)
	k8s := fake.NewClientBuilder().WithScheme(scheme).WithRuntimeObjects(team("alpha-team")).Build()
	failing := &failingListClient{Client: k8s}
	var o oss.StorageClient = &mcLikeOSS{Memory: store}
	h := NewProjectHandler(failing, "default", o)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/workflow", nil)
	req.SetPathValue("id", "p1")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status=%d, want 500 on K8s failure", rec.Code)
	}
}

// failingListClient fails every K8s List call.
type failingListClient struct {
	client.Client
}

func (f *failingListClient) List(_ context.Context, _ client.ObjectList, _ ...client.ListOption) error {
	return errors.New("k8s list failed")
}

// TestGetProjectWorkflow_GetObjectErrorReturns500 guards the meta read
// failure path: a non-NotFound GetObject error surfaces as 500 (not a
// silently skipped project / false 404).
func TestGetProjectWorkflow_GetObjectErrorReturns500(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "teams/alpha-team/shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1", "title": "P1", "status": "active", "plan_type": "dag", "team_id": "alpha-team",
	})
	m := &mcLikeOSS{Memory: store, failGet: true}
	scheme := newProjectTestScheme(t)
	k8s := fake.NewClientBuilder().WithScheme(scheme).WithRuntimeObjects(team("alpha-team")).Build()
	h := NewProjectHandler(k8s, "default", m)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/workflow", nil)
	req.SetPathValue("id", "p1")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status=%d, want 500 on GetObject failure", rec.Code)
	}
}

// TestGetProjectWorkflow_ProjectInLaterPrefix guards multi-prefix resolution:
// resolveProjectMeta scans prefixes in order and finds the project when it
// lives under a later team prefix (alpha empty, beta holds it).
func TestGetProjectWorkflow_ProjectInLaterPrefix(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "teams/beta-team/shared/projects/p1/meta.json", map[string]any{
		"project_id": "p1", "title": "P1", "status": "active", "plan_type": "dag", "team_id": "beta-team",
		"tasks": []map[string]any{{"task_id": "t1", "title": "T1", "status": "planned", "depends_on": []string{}}},
	})
	m := &mcLikeOSS{Memory: store}
	scheme := newProjectTestScheme(t)
	k8s := fake.NewClientBuilder().WithScheme(scheme).WithRuntimeObjects(team("alpha-team"), team("beta-team")).Build()
	h := NewProjectHandler(k8s, "default", m)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/p1/workflow", nil)
	req.SetPathValue("id", "p1")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d, want 200 (project found in later prefix); body=%s", rec.Code, rec.Body.String())
	}
	var wf workflowResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &wf); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if wf.TeamID != "beta-team" {
		t.Fatalf("team_id=%q, want beta-team", wf.TeamID)
	}
}

// TestListProjects_L2HumanTeamFilter guards the L2 + ?team= combination: an
// L2 human aggregating two teams can narrow to one of their own teams; asking
// for a team outside the accessible set returns nothing (never leaks).
func TestListProjects_L2HumanTeamFilter(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "teams/alpha-team/shared/projects/pa/meta.json", map[string]any{
		"project_id": "pa", "title": "PA", "status": "active", "plan_type": "dag", "team_id": "alpha-team",
	})
	putProject(store, "teams/beta-team/shared/projects/pb/meta.json", map[string]any{
		"project_id": "pb", "title": "PB", "status": "active", "plan_type": "dag", "team_id": "beta-team",
	})
	h := newProjectTestHandler(t, store, team("alpha-team"), team("beta-team"))
	l2 := &authpkg.CallerIdentity{
		Role: authpkg.RoleHuman, Username: "maizong", Teams: []string{"alpha-team", "beta-team"},
	}

	// Narrow to one accessible team.
	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects?team=alpha-team", nil)
	req = withCaller(req, l2)
	rec := httptest.NewRecorder()
	h.ListProjects(rec, req)
	var resp struct {
		Projects []map[string]any `json:"projects"`
		Total    int              `json:"total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Total != 1 || resp.Projects[0]["project_id"] != "pa" {
		t.Fatalf("team filter (own team) wrong: %+v", resp.Projects)
	}

	// Ask for a team outside the accessible set -> nothing.
	req2 := httptest.NewRequest(http.MethodGet, "/api/v1/projects?team=gamma-team", nil)
	req2 = withCaller(req2, l2)
	rec2 := httptest.NewRecorder()
	h.ListProjects(rec2, req2)
	var resp2 struct {
		Projects []map[string]any `json:"projects"`
		Total    int              `json:"total"`
	}
	if err := json.Unmarshal(rec2.Body.Bytes(), &resp2); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp2.Total != 0 {
		t.Fatalf("team filter (outside accessible) leaked %d projects: %+v", resp2.Total, resp2.Projects)
	}
}

// staticWhoami implements authpkg.MatrixWhoami for the HTTP auth-chain test.
type staticWhoami struct {
	validToken string
	userID     string
}

func (s *staticWhoami) Whoami(_ context.Context, token string) (string, error) {
	if token != s.validToken {
		return "", errors.New("invalid matrix token")
	}
	return s.userID, nil
}

// alwaysFailAuth always fails (simulates SA TokenReview rejecting a Matrix token).
type alwaysFailAuth struct{}

func (a *alwaysFailAuth) Authenticate(_ context.Context, _ string) (*authpkg.CallerIdentity, error) {
	return nil, errors.New("SA token review failed")
}

// TestProjectHTTP_L2AuthChain exercises the full HTTP chain — bearer token
// extraction, composite authentication (SA fails, Matrix whoami succeeds),
// identity enrichment, authorization, and the project handler — for the L2
// human path.
func TestProjectHTTP_L2AuthChain(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "teams/alpha-team/shared/projects/pa/meta.json", map[string]any{
		"project_id": "pa", "title": "PA", "status": "active", "plan_type": "dag", "team_id": "alpha-team",
	})
	scheme := newProjectTestScheme(t)
	human := &v1beta1.Human{
		ObjectMeta: metav1.ObjectMeta{Name: "maizong", Namespace: "default"},
		Spec:       v1beta1.HumanSpec{Username: "maizong", PermissionLevel: 2, AccessibleTeams: []string{"alpha-team"}},
	}
	k8s := fake.NewClientBuilder().WithScheme(scheme).
		WithRuntimeObjects(human, team("alpha-team")).Build()

	matrixAuth := authpkg.NewMatrixTokenAuthenticator(k8s, "default", &staticWhoami{validToken: "matrix-token", userID: "@maizong:matrix.local"})
	composite := authpkg.NewCompositeAuthenticator(&alwaysFailAuth{}, matrixAuth)
	enricher := authpkg.NewCREnricher(k8s, "default")
	mw := authpkg.NewMiddleware(composite, enricher, authpkg.NewAuthorizer(), k8s, "default")

	var ossStore oss.StorageClient = &mcLikeOSS{Memory: store}
	srv := NewHTTPServer(":0", ServerDeps{
		Client:    k8s,
		Namespace: "default",
		OSS:       ossStore,
		AuthMw:    mw,
	})

	// L2 human with Matrix token -> aggregated list for accessible team.
	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	req.Header.Set("Authorization", "Bearer matrix-token")
	rec := httptest.NewRecorder()
	srv.Mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("L2 list status=%d, want 200; body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"project_id":"pa"`) {
		t.Fatalf("expected pa in L2 list, got %s", rec.Body.String())
	}

	// Invalid token -> 401.
	req2 := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	req2.Header.Set("Authorization", "Bearer bad-token")
	rec2 := httptest.NewRecorder()
	srv.Mux.ServeHTTP(rec2, req2)
	if rec2.Code != http.StatusUnauthorized {
		t.Fatalf("bad token status=%d, want 401", rec2.Code)
	}

	// No token -> 401.
	req3 := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	rec3 := httptest.NewRecorder()
	srv.Mux.ServeHTTP(rec3, req3)
	if rec3.Code != http.StatusUnauthorized {
		t.Fatalf("no token status=%d, want 401", rec3.Code)
	}

	// L2 human reads a workflow via the same HTTP chain (accessible team).
	req4 := httptest.NewRequest(http.MethodGet, "/api/v1/projects/pa/workflow", nil)
	req4.SetPathValue("id", "pa")
	req4.Header.Set("Authorization", "Bearer matrix-token")
	rec4 := httptest.NewRecorder()
	srv.Mux.ServeHTTP(rec4, req4)
	if rec4.Code != http.StatusOK {
		t.Fatalf("L2 workflow status=%d, want 200; body=%s", rec4.Code, rec4.Body.String())
	}

	// W4: L2 human requests a project from a non-accessible team through the
	// full HTTP chain -> 404 (existence hidden, same as a missing project).
	putProject(store, "teams/beta-team/shared/projects/pb/meta.json", map[string]any{
		"project_id": "pb", "title": "PB", "status": "active", "plan_type": "dag", "team_id": "beta-team",
	})
	req5 := httptest.NewRequest(http.MethodGet, "/api/v1/projects/pb/workflow", nil)
	req5.SetPathValue("id", "pb")
	req5.Header.Set("Authorization", "Bearer matrix-token")
	rec5 := httptest.NewRecorder()
	srv.Mux.ServeHTTP(rec5, req5)
	if rec5.Code != http.StatusNotFound {
		t.Fatalf("L2 cross-team workflow status=%d, want 404 (W4: hide existence)", rec5.Code)
	}
}


// TestGetProjectWorkflow_PassThroughAuditFields guards W2: human-intervention
// audit fields written by W-PR-2 (updated_by/updated_at/pause_reason) are
// passed through the workflow response.
func TestGetProjectWorkflow_PassThroughAuditFields(t *testing.T) {
	store := ossfake.NewMemory()
	putProject(store, "shared/projects/audit1/meta.json", map[string]any{
		"project_id": "audit1", "title": "Audit", "status": "paused", "plan_type": "dag",
		"updated_by": "luo", "updated_at": "2026-08-12T10:00:00Z", "pause_reason": "hold for review",
		"tasks": []map[string]any{},
	})
	h := newProjectTestHandler(t, store)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects/audit1/workflow", nil)
	req.SetPathValue("id", "audit1")
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleManager, Username: "manager"})
	rec := httptest.NewRecorder()
	h.GetProjectWorkflow(rec, req)

	var wf workflowResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &wf); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if wf.UpdatedBy != "luo" || wf.UpdatedAt != "2026-08-12T10:00:00Z" || wf.PauseReason != "hold for review" {
		t.Fatalf("audit fields not passed through: %+v", wf)
	}
}

// TestListProjects_ConcurrentFetch guards W7: the concurrent GetObject pool
// returns the same complete, deduplicated list as serial iteration would.
func TestListProjects_ConcurrentFetch(t *testing.T) {
	store := ossfake.NewMemory()
	for i := 0; i < 20; i++ {
		id := fmt.Sprintf("cp%02d", i)
		putProject(store, "shared/projects/"+id+"/meta.json", map[string]any{
			"project_id": id, "title": "CP " + id, "status": "active", "plan_type": "dag",
		})
	}
	// One duplicate across prefixes should be deduplicated.
	putProject(store, "teams/alpha-team/shared/projects/cp01/meta.json", map[string]any{
		"project_id": "cp01", "title": "CP cp01", "status": "active", "plan_type": "dag", "team_id": "alpha-team",
	})
	h := newProjectTestHandler(t, store, team("alpha-team"))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/projects", nil)
	req = withCaller(req, &authpkg.CallerIdentity{Role: authpkg.RoleAdmin, Username: "admin"})
	rec := httptest.NewRecorder()
	h.ListProjects(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var resp struct {
		Projects []map[string]any `json:"projects"`
		Total    int              `json:"total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Total != 20 {
		t.Fatalf("total=%d, want 20 (20 unique projects, 1 deduped)", resp.Total)
	}
	// Deterministic ordering preserved.
	last := ""
	for _, p := range resp.Projects {
		id, _ := p["project_id"].(string)
		if last != "" && last > id {
			t.Fatalf("not sorted: %s > %s", last, id)
		}
		last = id
	}
}
