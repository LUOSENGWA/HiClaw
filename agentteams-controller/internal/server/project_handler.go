package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"

	v1beta1 "github.com/agentscope-ai/AgentTeams/agentteams-controller/api/v1beta1"
	authpkg "github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/auth"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/httputil"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/oss"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// ProjectHandler exposes project / workflow data stored in object storage
// (shared/projects/{id}/meta.json) so humans and frontends can inspect and
// (optionally) intervene in agent-orchestrated workflows.
//
// The TeamHarness MCP projectflow/taskflow actions run inside the worker
// process (stdio JSON-RPC), so the Controller cannot call them directly.
// Instead this handler reads the same MinIO objects the workers sync.
//
// Response schema is aligned with LangGraph Graph.to_json() / StateSnapshot
// (MIT License, © LangChain, Inc.) — see the W-PR design doc.
type ProjectHandler struct {
	client    client.Client
	namespace string
	oss       oss.StorageClient
}

// NewProjectHandler creates a handler reading project state from object storage.
func NewProjectHandler(c client.Client, namespace string, o oss.StorageClient) *ProjectHandler {
	return &ProjectHandler{client: c, namespace: namespace, oss: o}
}

// --- internal model (mirrors meta.json, tolerant to extra fields) ---

type projectMeta struct {
	ProjectID       string            `json:"project_id"`
	Title           string            `json:"title"`
	Status          string            `json:"status"`
	PlanType        string            `json:"plan_type"`
	TeamID          string            `json:"team_id"`
	Mode            string            `json:"mode"`
	Source          string            `json:"source,omitempty"`
	Tasks           []projectTaskMeta `json:"tasks"`
	Loop            *loopMeta         `json:"loop,omitempty"`
	Requester       string            `json:"requester,omitempty"`
	RequesterReport map[string]any    `json:"requester_report,omitempty"`
	ReplyRoute      map[string]any    `json:"reply_route,omitempty"`
	SourceRoomID    string            `json:"source_room_id,omitempty"`
	// W2: human-intervention audit fields (written by W-PR-2 Controller API;
	// tolerated by json.Unmarshal when absent, and passed through here so
	// consumers can show who paused/resumed and why).
	UpdatedBy   string `json:"updated_by,omitempty"`
	UpdatedAt   string `json:"updated_at,omitempty"`
	PauseReason string `json:"pause_reason,omitempty"`
}

type projectTaskMeta struct {
	TaskID     string   `json:"task_id"`
	Title      string   `json:"title"`
	AssignedTo string   `json:"assigned_to"`
	DependsOn  []string `json:"depends_on"`
	Status     string   `json:"status"`
}

type loopMeta struct {
	Goal              string            `json:"goal"`
	StopCondition     string            `json:"stop_condition"`
	IterationTemplate string            `json:"iteration_template,omitempty"`
	CurrentIteration  int               `json:"current_iteration"`
	MaxIterations     int               `json:"max_iterations"`
	Status            string            `json:"status"`
	Tasks             []projectTaskMeta `json:"tasks,omitempty"`
	History           []json.RawMessage `json:"history,omitempty"`
}

// --- workflow response (LangGraph-aligned) ---

type workflowResponse struct {
	ProjectID       string              `json:"project_id"`
	Title           string              `json:"title"`
	Status          string              `json:"status"`
	PlanType        string              `json:"plan_type"`
	TeamID          string              `json:"team_id"`
	Mode            string              `json:"mode"`
	Source          string              `json:"source,omitempty"`
	Nodes           []workflowNode      `json:"nodes"`
	Edges           []workflowEdge      `json:"edges"`
	Next            []string            `json:"next"`
	Interrupts      []workflowInterrupt `json:"interrupts"`
	Values          *workflowValues     `json:"values,omitempty"`
	Loop            *loopMeta           `json:"loop,omitempty"`
	Requester       string              `json:"requester,omitempty"`
	RequesterReport map[string]any      `json:"requester_report,omitempty"`
	ReplyRoute      map[string]any      `json:"reply_route,omitempty"`
	SourceRoomID    string              `json:"source_room_id,omitempty"`
	// TasksDetail is populated only when ?includeTasks=true; otherwise it is
	// omitted so the default workflow response stays lightweight.
	TasksDetail []taskDetail `json:"tasks_detail,omitempty"`
	// W2: human-intervention audit fields.
	UpdatedBy   string `json:"updated_by,omitempty"`
	UpdatedAt   string `json:"updated_at,omitempty"`
	PauseReason string `json:"pause_reason,omitempty"`
}

// workflowValues is the current state summary (LangGraph StateSnapshot.values
// analog): project-level fields plus a per-status task count.
type workflowValues struct {
	ProjectID string         `json:"project_id"`
	Title     string         `json:"title"`
	Status    string         `json:"status"`
	PlanType  string         `json:"plan_type"`
	TeamID    string         `json:"team_id"`
	Mode      string         `json:"mode"`
	TaskCount map[string]int `json:"task_count"`
}

type workflowNode struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Status   string `json:"status"`
	Assignee string `json:"assignee,omitempty"`
}

type workflowEdge struct {
	Source      string `json:"source"`
	Target      string `json:"target"`
	Conditional bool   `json:"conditional"`
}

type workflowInterrupt struct {
	ID    string `json:"id"`
	Value string `json:"value"`
}

// taskDetail is the per-task detail payload returned when
// ?includeTasks=true. It surfaces TaskMeta (shared/tasks/{id}/meta.json)
// fields that are not part of the project-level tasks[] node summary: the
// spec path, submission summary/result status, deliverables list and result
// path. TaskMeta is written by TeamHarness taskflow (delegate_task creates
// spec.md + meta.json, submit_task adds summary/result_status/deliverables/
// result_path) and pushed to shared storage via _sync_task, so the same
// dual-prefix scan used for projects applies here.
type taskDetail struct {
	TaskID       string     `json:"task_id"`
	ProjectID    string     `json:"project_id,omitempty"`
	Status       string     `json:"status,omitempty"`
	SpecPath     string     `json:"spec_path,omitempty"`
	AssignedTo   string     `json:"assigned_to,omitempty"`
	Summary      string     `json:"summary,omitempty"`
	ResultStatus string     `json:"result_status,omitempty"`
	Deliverables []any      `json:"deliverables,omitempty"`
	ResultPath   string     `json:"result_path,omitempty"`
	CancelReason string     `json:"cancel_reason,omitempty"`
}

// normalizeTaskStatus maps ProjectMeta task status to the frontend-friendly
// enum: pending | delegated | in-progress | completed | revision | blocked.
//
// Project task nodes are updated by TeamHarness taskflow through their full
// lifecycle (server.py _update_project_task): planned (create/plan_dag) →
// assigned (delegate_task) → in_progress (ack_task) → submitted (submit_task)
// → completed/revision (accept_task_result), plus cancelled (cancel_task).
func normalizeTaskStatus(status string) string {
	switch status {
	case "planned", "":
		return "pending"
	case "assigned":
		return "delegated"
	case "in_progress", "submitted":
		return "in-progress"
	case "completed":
		return "completed"
	case "revision":
		return "revision"
	case "blocked", "cancelled":
		return "blocked"
	default:
		return "pending"
	}
}

// --- prefix resolution ---

// teamProjectPrefixes returns the MinIO prefixes that hold project metadata.
// Team members sync shared/ to teams/{team}/shared/, standalone agents use the
// global shared/ prefix (see sync.py and runtime_config.go SharedPrefix).
//
// The storage prefix uses TeamSpec.EffectiveTeamName (spec.teamName when set,
// else the Team CR name) — the same value EnsureTeamStorage uses. However
// workers resolve their team from the Worker API response (resp.Team = CR
// name), so when spec.teamName differs from the CR name we enumerate BOTH
// prefixes to tolerate the mismatch. The second return maps CR name →
// effective team name for TeamLeader scoping.
func (h *ProjectHandler) teamProjectPrefixes(ctx context.Context) ([]string, map[string]string, error) {
	var teams v1beta1.TeamList
	if err := h.client.List(ctx, &teams, client.InNamespace(h.namespace)); err != nil {
		return nil, nil, err
	}
	prefixes := make([]string, 0, len(teams.Items)+1)
	crToEffective := make(map[string]string, len(teams.Items))
	seen := map[string]bool{}
	for i := range teams.Items {
		effective := teams.Items[i].Spec.EffectiveTeamName(teams.Items[i].Name)
		crToEffective[teams.Items[i].Name] = effective
		for _, name := range []string{effective, teams.Items[i].Name} {
			prefix := "teams/" + name + "/shared/projects/"
			if !seen[prefix] {
				seen[prefix] = true
				prefixes = append(prefixes, prefix)
			}
		}
	}
	prefixes = append(prefixes, "shared/projects/")
	return prefixes, crToEffective, nil
}

// metaKeyFromListResult builds the full object key (relative to the storage
// prefix) for a project's meta.json from an mc ls child entry.
//
// oss.ListObjects runs `mc ls <prefix>` and returns the bare child names —
// project directories end with "/" (e.g. "demo-project-001/"). The meta.json
// lives one level below, so the full key is prefix + dir + "meta.json".
func metaKeyFromListResult(prefix, child string) (string, bool) {
	if !strings.HasSuffix(child, "/") {
		return "", false
	}
	return prefix + child + "meta.json", true
}

// resolveProjectMeta locates and reads a project's meta.json across the given
// prefixes. Returns the meta and the owning team ("" for global shared/
// projects).
//
// prefixes must come from teamProjectPrefixes so callers that also need the
// crToEffective map (e.g. for access checks) can share a single K8s List call
// instead of paying two round-trips per request.
func (h *ProjectHandler) resolveProjectMeta(ctx context.Context, projectID string, prefixes []string) (*projectMeta, string, error) {
	for _, prefix := range prefixes {
		children, err := h.oss.ListObjects(ctx, prefix)
		if err != nil {
			return nil, "", err
		}
		for _, child := range children {
			key, ok := metaKeyFromListResult(prefix, child)
			if !ok || child != projectID+"/" {
				continue
			}
			data, err := h.oss.GetObject(ctx, key)
			if err != nil {
				if errors.Is(err, os.ErrNotExist) {
					continue // project dir exists but meta.json not yet written
				}
				return nil, "", err
			}
			var meta projectMeta
			if err := json.Unmarshal(data, &meta); err != nil {
				// meta.json is written non-atomically upstream (write_text);
				// a crash can leave truncated JSON. Treat as not found for
				// this prefix and keep scanning rather than 500.
				continue
			}
			team := teamFromPrefix(prefix)
			if meta.TeamID == "" {
				meta.TeamID = team
			}
			return &meta, team, nil
		}
	}
	return nil, "", nil
}

// teamFromPrefix extracts the team name from a project prefix, or "" for the
// global shared/ prefix.
func teamFromPrefix(prefix string) string {
	if strings.HasPrefix(prefix, "teams/") {
		parts := strings.SplitN(prefix, "/", 3)
		if len(parts) == 3 {
			return parts[1]
		}
	}
	return ""
}

// checkProjectAccess performs the handler-side team check for scoped readers
// (team leaders and L2 humans). They may only access projects owned by their
// team(s) (team-scoped prefix). Global projects and other teams' projects are
// denied.
//
// caller.Team is the legacy single Team CR name; caller.Teams is the L2 human
// multi-team set (Human CR accessibleTeams, CR names). crToEffective
// translates each to the effective storage team name (spec.teamName may
// differ from the CR name), so both the CR name and the effective name match.
func (h *ProjectHandler) checkProjectAccess(caller *authpkg.CallerIdentity, team string, crToEffective map[string]string) error {
	if caller == nil || (caller.Role != authpkg.RoleTeamLeader && caller.Role != authpkg.RoleHuman) {
		return nil
	}
	teams := caller.Teams
	if len(teams) == 0 && caller.Team != "" {
		teams = []string{caller.Team}
	}
	for _, t := range teams {
		eff := t
		if mapped, ok := crToEffective[t]; ok && mapped != "" {
			eff = mapped
		}
		if eff == team {
			return nil
		}
	}
	return &accessDeniedError{msg: "team-leader cannot access project outside team " + caller.Team}
}

// callerAccessiblePrefixes expands a scoped reader's accessible teams (legacy
// single Team or L2 human multi-team set) into the set of project prefixes
// they may read. Projects live under the effective storage name
// (TeamSpec.EffectiveTeamName via EnsureTeamStorage), so each accessible CR
// name maps to its effective prefix; an unresolvable team falls back to its
// own name.
func callerAccessiblePrefixes(caller *authpkg.CallerIdentity, crToEffective map[string]string) map[string]bool {
	if caller == nil || (caller.Role != authpkg.RoleTeamLeader && caller.Role != authpkg.RoleHuman) {
		return nil
	}
	teams := caller.Teams
	if len(teams) == 0 && caller.Team != "" {
		teams = []string{caller.Team}
	}
	out := make(map[string]bool, len(teams))
	for _, t := range teams {
		eff := t
		if mapped, ok := crToEffective[t]; ok && mapped != "" {
			eff = mapped
		}
		out["teams/"+eff+"/shared/projects/"] = true
	}
	return out
}

type accessDeniedError struct{ msg string }

func (e *accessDeniedError) Error() string { return e.msg }

// --- handlers ---

// GET /api/v1/projects
func (h *ProjectHandler) ListProjects(w http.ResponseWriter, r *http.Request) {
	caller := authpkg.CallerFromContext(r.Context())
	teamFilter := r.URL.Query().Get("team")

	prefixes, crToEffective, err := h.teamProjectPrefixes(r.Context())
	if err != nil {
		writeK8sError(w, "list projects: resolve prefixes", err)
		return
	}

	type projectSummary struct {
		ProjectID string `json:"project_id"`
		Title     string `json:"title"`
		Status    string `json:"status"`
		PlanType  string `json:"plan_type"`
		TeamID    string `json:"team_id"`
		Mode      string `json:"mode"`
	}
	projects := make([]projectSummary, 0)
	seen := map[string]bool{}

	// Team leaders (legacy single-team SA or L2 human multi-team) only scan
	// their accessible prefixes. The caller's Teams are Team CR names;
	// crToEffective expands to the effective storage names too.
	accessible := callerAccessiblePrefixes(caller, crToEffective)

	// W7: collect all candidate keys first, then fetch meta.json concurrently.
	// Each GetObject is a separate mc subprocess, so N projects would
	// otherwise pay N process spawns serially. A small worker pool collapses
	// that to ~ceil(N/concurrency) rounds.
	type candidate struct {
		prefix string
		key    string
	}
	var cands []candidate
	for _, prefix := range prefixes {
		if accessible != nil && !accessible[prefix] {
			continue
		}
		// ?team= filter: skip prefixes that cannot hold the requested team
		// before hitting OSS (O2). teamFromPrefix("shared/projects/") is ""
		// and standalone projects only match when no filter is set — identical
		// to the meta-level filter below, so this is a pure early-exit.
		if teamFilter != "" && teamFromPrefix(prefix) != teamFilter {
			continue
		}
		children, err := h.oss.ListObjects(r.Context(), prefix)
		if err != nil {
			writeK8sError(w, "list projects", err)
			return
		}
		for _, child := range children {
			key, ok := metaKeyFromListResult(prefix, child)
			if !ok {
				continue
			}
			cands = append(cands, candidate{prefix: prefix, key: key})
		}
	}

	const listConcurrency = 8
	type result struct {
		candidate
		data []byte
		err  error
	}
	results := make([]result, len(cands))
	sem := make(chan struct{}, listConcurrency)
	var wg sync.WaitGroup
	for i, c := range cands {
		wg.Add(1)
		sem <- struct{}{}
		go func(i int, c candidate) {
			defer wg.Done()
			defer func() { <-sem }()
			data, err := h.oss.GetObject(r.Context(), c.key)
			results[i] = result{candidate: c, data: data, err: err}
		}(i, c)
	}
	wg.Wait()

	for _, res := range results {
		if res.err != nil {
			if errors.Is(res.err, os.ErrNotExist) {
				continue // project dir without meta.json yet; skip, not 500
			}
			// W5: a single project read failure must not fail the whole
			// list (one bad/transient object would 500 everything else).
			// Infrastructure-level failures (ListObjects) still 500;
			// per-object data failures are skipped like malformed meta.
			continue
		}
		var meta projectMeta
		if err := json.Unmarshal(res.data, &meta); err != nil {
			continue // skip malformed meta instead of failing the whole list
		}
		if meta.ProjectID == "" || seen[meta.ProjectID] {
			continue
		}
		seen[meta.ProjectID] = true
		team := teamFromPrefix(res.prefix)
		if meta.TeamID == "" {
			meta.TeamID = team
		}
		// Optional ?team= filter (mirrors ListWorkers). Team leaders are
		// already scoped by their own prefix; standalone projects have an
		// empty team and are only matched when no filter is set.
		if teamFilter != "" && meta.TeamID != teamFilter {
			continue
		}
		projects = append(projects, projectSummary{
			ProjectID: meta.ProjectID,
			Title:     meta.Title,
			Status:    meta.Status,
			PlanType:  meta.PlanType,
			TeamID:    meta.TeamID,
			Mode:      meta.Mode,
		})
	}

	// Deterministic ordering across prefixes.
	sort.Slice(projects, func(i, j int) bool { return projects[i].ProjectID < projects[j].ProjectID })

	httputil.WriteJSON(w, http.StatusOK, map[string]any{"projects": projects, "total": len(projects)})
}

// GET /api/v1/projects/{id}/workflow
func (h *ProjectHandler) GetProjectWorkflow(w http.ResponseWriter, r *http.Request) {
	projectID := r.PathValue("id")
	if projectID == "" {
		httputil.WriteError(w, http.StatusBadRequest, "project id is required")
		return
	}
	caller := authpkg.CallerFromContext(r.Context())
	includeTasks := r.URL.Query().Get("includeTasks") == "true"

	// Single K8s List for both meta resolution and the access check (O1).
	prefixes, crToEffective, err := h.teamProjectPrefixes(r.Context())
	if err != nil {
		writeK8sError(w, "get project workflow: resolve prefixes", err)
		return
	}
	meta, team, err := h.resolveProjectMeta(r.Context(), projectID, prefixes)
	if err != nil {
		writeK8sError(w, "get project workflow", err)
		return
	}
	if meta == nil {
		httputil.WriteError(w, http.StatusNotFound, "project not found")
		return
	}
	// W4: hide project existence from scoped callers (L2 / team leader) who
	// do not own this project. resolveProjectMeta scans all prefixes, so a
	// cross-team project is found; returning 403 would let callers enumerate
	// other teams' project ids. Return 404 to hide existence (same as a
	// non-existent id). Admin/Manager (checkProjectAccess returns nil) are
	// unaffected.
	if err := h.checkProjectAccess(caller, team, crToEffective); err != nil {
		if _, ok := err.(*accessDeniedError); ok {
			httputil.WriteError(w, http.StatusNotFound, "project not found")
			return
		}
		httputil.WriteError(w, http.StatusForbidden, err.Error())
		return
	}

	httputil.WriteJSON(w, http.StatusOK, h.buildWorkflow(meta, team, includeTasks))
}

// buildWorkflow converts project meta into the LangGraph-aligned response.
// When includeTasks is true it additionally reads per-task TaskMeta from
// shared storage and attaches tasks_detail.
func (h *ProjectHandler) buildWorkflow(meta *projectMeta, team string, includeTasks bool) *workflowResponse {
	nodes := make([]workflowNode, 0, len(meta.Tasks))
	edges := make([]workflowEdge, 0)
	next := make([]string, 0)
	interrupts := make([]workflowInterrupt, 0)

	// For loop plans the executable graph lives in loop.tasks; otherwise in
	// project.tasks. Mirrors _ready_nodes / _ready_loop_nodes semantics.
	graphTasks := meta.Tasks
	if meta.PlanType == "loop" && meta.Loop != nil {
		graphTasks = meta.Loop.Tasks
	}

	completed := map[string]bool{}
	for _, t := range graphTasks {
		if t.Status == "completed" {
			completed[t.TaskID] = true
		}
		if t.Status == "blocked" || t.Status == "cancelled" {
			interrupts = append(interrupts, workflowInterrupt{ID: t.TaskID, Value: "blocked"})
		}
		nodes = append(nodes, workflowNode{
			ID:       t.TaskID,
			Name:     t.Title,
			Status:   normalizeTaskStatus(t.Status),
			Assignee: t.AssignedTo,
		})
		for _, dep := range t.DependsOn {
			edges = append(edges, workflowEdge{Source: dep, Target: t.TaskID, Conditional: false})
		}
	}

	// Ready nodes: only for active projects (and an active loop); tasks
	// pending/delegated whose dependencies are all completed (mirrors
	// _ready_nodes / _ready_loop_nodes semantics).
	loopBlocked := false
	if meta.Loop != nil {
		if meta.Loop.Status == "completed" || meta.Loop.Status == "blocked" || meta.Loop.Status == "waiting_user" {
			loopBlocked = true
		}
	}
	if !loopBlocked && (meta.Status == "" || meta.Status == "active") {
		for _, t := range graphTasks {
			// Mirror upstream _ready_nodes/_ready_loop_nodes exactly: only
			// tasks whose raw status is planned/assigned can be ready.
			// Checking the raw status (not the normalized output) avoids
			// treating "" or unknown statuses as pending — upstream skips
			// those, so a consumer must not see them as executable.
			if t.Status != "planned" && t.Status != "assigned" {
				continue
			}
			allDone := true
			for _, dep := range t.DependsOn {
				if !completed[dep] {
					allDone = false
					break
				}
			}
			if allDone {
				next = append(next, t.TaskID)
			}
		}
	}

	// Loop interrupts mirror _ready_loop_nodes: a loop waiting on a human
	// decision or blocked surfaces as an interrupt.
	if meta.Loop != nil {
		if meta.Loop.Status == "waiting_user" {
			interrupts = append(interrupts, workflowInterrupt{ID: "loop", Value: "waiting for human decision"})
		} else if meta.Loop.Status == "blocked" {
			interrupts = append(interrupts, workflowInterrupt{ID: "loop", Value: "blocked"})
		}
	}

	// W1: a paused project is a human interrupt in LangGraph terms — the
	// workflow is suspended awaiting a human decision (resume). Surfacing it
	// as an interrupt (in addition to status=paused) lets consumers show
	// "paused by human" without parsing project status separately.
	if meta.Status == "paused" {
		interrupts = append(interrupts, workflowInterrupt{ID: "project", Value: "paused"})
	}

	// values: current state summary (LangGraph StateSnapshot.values analog).
	taskCount := map[string]int{}
	for _, n := range nodes {
		taskCount[n.Status]++
	}

	// includeTasks: read per-task TaskMeta (shared/tasks/{id}/meta.json)
	// from the same dual-prefix layout as projects and attach tasks_detail.
	// Tasks without a TaskMeta file are skipped (the node summary remains
	// authoritative); per-task read errors are skipped too so one bad task
	// does not fail the whole workflow response.
	var tasksDetail []taskDetail
	if includeTasks {
		tasksDetail = h.readTasksDetail(meta, team)
	}

	return &workflowResponse{
		ProjectID:  meta.ProjectID,
		Title:      meta.Title,
		Status:     meta.Status,
		PlanType:   meta.PlanType,
		TeamID:     meta.TeamID,
		Mode:       meta.Mode,
		Source:     meta.Source,
		Nodes:      nodes,
		Edges:      edges,
		Next:       next,
		Interrupts: interrupts,
		Values: &workflowValues{
			ProjectID: meta.ProjectID,
			Title:     meta.Title,
			Status:    meta.Status,
			PlanType:  meta.PlanType,
			TeamID:    meta.TeamID,
			Mode:      meta.Mode,
			TaskCount: taskCount,
		},
		Loop:            meta.Loop,
		Requester:       meta.Requester,
		RequesterReport: meta.RequesterReport,
		ReplyRoute:      meta.ReplyRoute,
		SourceRoomID:    meta.SourceRoomID,
		TasksDetail:     tasksDetail,
		UpdatedBy:       meta.UpdatedBy,
		UpdatedAt:       meta.UpdatedAt,
		PauseReason:     meta.PauseReason,
	}
}

// readTasksDetail reads TaskMeta (shared/tasks/{id}/meta.json) for every task
// in the project's graph and returns the detail list in node order.
//
// TaskMeta is stored under the same dual-prefix layout as projects
// (teams/{team}/shared/tasks/{id}/meta.json for team members, shared/tasks/
// {id}/meta.json for standalone workers) — _sync_task pushes the local
// shared/tasks/{id} directory after delegate/ack/submit/cancel. We probe the
// task prefix belonging to this project's team first, then the global
// prefix, mirroring resolveProjectMeta. Reads are concurrent (W7 pattern) so
// N tasks cost ~ceil(N/8) mc subprocess rounds instead of N serial spawns.
func (h *ProjectHandler) readTasksDetail(meta *projectMeta, team string) []taskDetail {
	// Collect unique task ids from the graph (project tasks, or loop tasks
	// for loop plans — same set buildWorkflow renders).
	graphTasks := meta.Tasks
	if meta.PlanType == "loop" && meta.Loop != nil {
		graphTasks = meta.Loop.Tasks
	}
	taskIDs := make([]string, 0, len(graphTasks))
	seen := map[string]bool{}
	for _, t := range graphTasks {
		if t.TaskID == "" || seen[t.TaskID] {
			continue
		}
		seen[t.TaskID] = true
		taskIDs = append(taskIDs, t.TaskID)
	}
	if len(taskIDs) == 0 {
		return nil
	}

	// Candidate TaskMeta keys: project team prefix first, global fallback.
	var keys []string
	for _, id := range taskIDs {
		if team != "" {
			keys = append(keys, "teams/"+team+"/shared/tasks/"+id+"/meta.json")
		}
		keys = append(keys, "shared/tasks/"+id+"/meta.json")
	}

	const detailConcurrency = 8
	type keyResult struct {
		taskID string
		key    string
		data   []byte
	}
	results := make([]keyResult, len(keys))
	sem := make(chan struct{}, detailConcurrency)
	var wg sync.WaitGroup
	for i, key := range keys {
		wg.Add(1)
		sem <- struct{}{}
		go func(i int, taskID, key string) {
			defer wg.Done()
			defer func() { <-sem }()
			data, err := h.oss.GetObject(context.Background(), key)
			if err != nil {
				results[i] = keyResult{taskID: taskID, key: key}
				return
			}
			results[i] = keyResult{taskID: taskID, key: key, data: data}
		}(i, keyForTaskID(key, taskIDs), key)
	}
	wg.Wait()

	// First non-empty match per task id wins (team prefix takes precedence
	// because it is listed first; a task published to the global prefix is a
	// fallback for standalone projects).
	detailByTask := make(map[string]taskDetail, len(taskIDs))
	for _, res := range results {
		if len(res.data) == 0 {
			continue
		}
		if _, ok := detailByTask[res.taskID]; ok {
			continue
		}
		var raw map[string]any
		if err := json.Unmarshal(res.data, &raw); err != nil {
			continue // malformed TaskMeta; keep node summary only
		}
		detail := taskDetail{
			TaskID:       str(taskIDFromRaw(raw, res.taskID)),
			Status:       str(raw["status"]),
			SpecPath:     str(raw["spec_path"]),
			AssignedTo:   str(raw["assigned_to"]),
			Summary:      str(raw["summary"]),
			ResultStatus: str(raw["result_status"]),
			ResultPath:   str(raw["result_path"]),
			CancelReason: str(raw["cancel_reason"]),
		}
		if raw["project_id"] != nil {
			detail.ProjectID = str(raw["project_id"])
		}
		if raw["deliverables"] != nil {
			if list, ok := raw["deliverables"].([]any); ok {
				detail.Deliverables = list
			}
		}
		detailByTask[res.taskID] = detail
	}

	// Return in graph order for stable output.
	out := make([]taskDetail, 0, len(taskIDs))
	for _, id := range taskIDs {
		if d, ok := detailByTask[id]; ok {
			out = append(out, d)
		}
	}
	return out
}

// keyForTaskID maps a candidate key back to the task id by extracting the
// directory component after ".../tasks/". All keys are built from taskIDs in
// readTasksDetail, so a simple suffix search is sufficient; if the lookup
// fails the key index order (team-first) still yields the right task id.
func keyForTaskID(key string, taskIDs []string) string {
	for _, id := range taskIDs {
		if strings.Contains(key, "/tasks/"+id+"/") {
			return id
		}
	}
	return ""
}

// str is a small helper to coerce a JSON value to string ("" for nil).
func str(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprintf("%v", v)
}

// taskIDFromRaw prefers the raw task_id field, falling back to the key-derived id.
func taskIDFromRaw(raw map[string]any, fallback string) string {
	if s := str(raw["task_id"]); s != "" {
		return s
	}
	return fallback
}
