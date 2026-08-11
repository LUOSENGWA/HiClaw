package server

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"sort"
	"strings"

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
			data, err := h.oss.GetObject(r.Context(), key)
			if err != nil {
				if errors.Is(err, os.ErrNotExist) {
					continue // project dir without meta.json yet; skip, not 500
				}
				writeK8sError(w, "list projects: read meta", err)
				return
			}
			var meta projectMeta
			if err := json.Unmarshal(data, &meta); err != nil {
				continue // skip malformed meta instead of failing the whole list
			}
			if meta.ProjectID == "" || seen[meta.ProjectID] {
				continue
			}
			seen[meta.ProjectID] = true
			team := teamFromPrefix(prefix)
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
	if err := h.checkProjectAccess(caller, team, crToEffective); err != nil {
		httputil.WriteError(w, http.StatusForbidden, err.Error())
		return
	}

	httputil.WriteJSON(w, http.StatusOK, h.buildWorkflow(meta))
}

// buildWorkflow converts project meta into the LangGraph-aligned response.
func (h *ProjectHandler) buildWorkflow(meta *projectMeta) *workflowResponse {
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

	// values: current state summary (LangGraph StateSnapshot.values analog).
	taskCount := map[string]int{}
	for _, n := range nodes {
		taskCount[n.Status]++
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
	}
}
