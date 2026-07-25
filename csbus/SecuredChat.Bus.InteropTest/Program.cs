using System.Diagnostics;
using SecuredChat.Bus;

// ---------------------------------------------------------------------------
// Live interop harness: the C# GitBusTransport against the REAL python CLI
// (SecuredChat/cli/chat.py). Not a mock, not a re-implementation of the peer —
// the actual tool, invoked as a subprocess, on shared git repos.
//
// Scenario 1 (local bus, no remote):   python init → C# send → python recv
//                                      sees it; python send → C# recv sees it;
//                                      cursor (since-id) semantics line up.
// Scenario 2 (bare remote, two clones): full cross-machine simulation.
// Scenario 3 (forced concurrent race):  python pushes while the C# peer has an
//                                      unpushed commit → C# push rejected →
//                                      pull --rebase union-merge → both lines
//                                      survive, both peers see both messages.
// ---------------------------------------------------------------------------

string chatPy = args.Length > 0 ? args[0] : "/home/claude/SecuredChat_up/SecuredChat/cli/chat.py";
if (!File.Exists(chatPy)) { Console.Error.WriteLine($"chat.py not found: {chatPy}"); return 1; }

string work = Path.Combine(Path.GetTempPath(), "csbus-interop-" + Guid.NewGuid().ToString("N")[..8]);
Directory.CreateDirectory(work);
int passed = 0, failed = 0;

void Check(bool ok, string what)
{
	if (ok) { passed++; Console.WriteLine($"  PASS  {what}"); }
	else { failed++; Console.WriteLine($"  FAIL  {what}"); }
}

(int Code, string Out, string Err) Run(string exe, string cwd, params string[] a)
{
	var psi = new ProcessStartInfo(exe) { WorkingDirectory = cwd, RedirectStandardOutput = true, RedirectStandardError = true };
	foreach (var x in a) psi.ArgumentList.Add(x);
	using var p = Process.Start(psi)!;
	string o = p.StandardOutput.ReadToEnd(); string e = p.StandardError.ReadToEnd();
	p.WaitForExit();
	return (p.ExitCode, o, e);
}

string pyHome = Path.Combine(work, "pyhome");   // isolates ~/.config/securedchat cursors per run
Directory.CreateDirectory(pyHome);
(int Code, string Out, string Err) Py(string bus, string identity, params string[] a)
{
	var psi = new ProcessStartInfo("python3") { WorkingDirectory = bus, RedirectStandardOutput = true, RedirectStandardError = true };
	psi.Environment["HOME"] = pyHome;
	foreach (var x in (string[])["-u", chatPy, "--bus", bus, "--room", "relay", "--identity", identity, .. a]) psi.ArgumentList.Add(x);
	using var p = Process.Start(psi)!;
	string o = p.StandardOutput.ReadToEnd(); string e = p.StandardError.ReadToEnd();
	p.WaitForExit();
	return (p.ExitCode, o, e);
}

void GitIn(string cwd, params string[] a)
{
	var r = Run("git", cwd, a);
	if (r.Code != 0) throw new Exception($"git {string.Join(' ', a)} failed in {cwd}: {r.Err}");
}

// ======================= Scenario 1: local bus, no remote ==================
Console.WriteLine("\n== Scenario 1: shared local bus (python init, both peers on one repo) ==");
string bus1 = Path.Combine(work, "bus-local");
Directory.CreateDirectory(bus1);
GitIn(bus1, "init", "-q");
GitIn(bus1, "config", "user.email", "test@local"); GitIn(bus1, "config", "user.name", "test");

var init = Py(bus1, "py-agent", "init");
Check(init.Code == 0 && init.Out.Contains("initialized"), $"python init created the room ({init.Out.Trim().Split('\n')[0]})");

// Anchor py-agent's cursor first: a FRESH identity's recv anchors at HEAD and
// skips history by design ("cold-cursor boot noise" guard) — the realistic flow
// is: peer comes online (recv anchors), THEN messages arrive.
var anchor = Py(bus1, "py-agent", "recv");
Check(anchor.Code == 0, "python recv anchors the fresh identity's cursor");

// C# → python, with a non-ASCII body to prove ensure_ascii=False parity.
var cs1 = new GitBusTransport(bus1, "relay", "cs-terminal");
var mOut = Message.New("cs-terminal", "py-agent", "hello from C# — Zürich sagt grüezi 🙂", kind: "cmd");
cs1.Send(mOut);
var pyRecv = Py(bus1, "py-agent", "recv", "--from-start", "--addressed-to-me", "--exclude-self");
Check(pyRecv.Code == 0 && pyRecv.Out.Contains("hello from C#") && pyRecv.Out.Contains("Zürich sagt grüezi 🙂"),
	"python recv sees the C# message, umlauts + emoji intact");
Check(pyRecv.Out.Contains("cs-terminal"), "python recv attributes it to identity 'cs-terminal'");

// python → C#, including a reply_to back-reference to the C# message id.
var pySend = Py(bus1, "py-agent", "send", "--to", "cs-terminal", "--reply-to", mOut.Id, "ack from python ✔");
Check(pySend.Code == 0, "python send --reply-to <C# id> succeeds");
var got = cs1.Recv(sinceId: mOut.Id);
Check(got.Count == 1 && got[0].Body == "ack from python ✔" && got[0].From == "py-agent",
	"C# recv(since: C# msg id) returns exactly the python reply");
Check(got[0].ReplyTo == mOut.Id, "reply_to round-trips: python's reply links the C# message id");

// Cursor semantics: full recv is 2, prefix cursor works, stale cursor is empty.
Check(cs1.Recv().Count == 2, "C# full recv sees both messages in order");
Check(cs1.Recv(mOut.Id[..8]).Count == 1, "C# recv with 8-char id prefix resolves like python's");
Check(cs1.Recv("ffffffff").Count == 0, "C# recv with a stale cursor returns nothing (no backlog replay)");

// Wire-format check by the strictest judge available: python's own parser.
string line = File.ReadLines(Path.Combine(bus1, "relay", "chat.jsonl")).First();
var pyParse = Run("python3", Path.GetDirectoryName(chatPy)!, "-c",
	$"import sys, json; sys.path.insert(0, '.'); from transport import Message\n" +
	$"m = Message.from_jsonl({System.Text.Json.JsonSerializer.Serialize(line)})\n" +
	$"assert m.from_ == 'cs-terminal' and m.to == 'py-agent' and m.kind == 'cmd', m\n" +
	$"assert m.id and m.ts > 0 and 'Zürich' in m.body, m\n" +
	$"print('python-parsed-ok')");
Check(pyParse.Out.Contains("python-parsed-ok"), "python transport.Message.from_jsonl parses the C#-written line field-for-field");

// ============== Scenario 2: bare remote + two clones (cross-machine) =======
Console.WriteLine("\n== Scenario 2: bare remote, python clone A ↔ C# clone B ==");
string remote = Path.Combine(work, "bus-remote.git");
Run("git", work, "init", "-q", "--bare", remote);
string cloneA = Path.Combine(work, "cloneA"); string cloneB = Path.Combine(work, "cloneB");
Run("git", work, "clone", "-q", remote, cloneA);
Run("git", work, "clone", "-q", remote, cloneB);
foreach (var c in new[] { cloneA, cloneB }) { GitIn(c, "config", "user.email", "t@t"); GitIn(c, "config", "user.name", "t"); }

var initA = Py(cloneA, "py-remote", "init");
Check(initA.Code == 0, "python init on clone A");
string branch = Run("git", cloneA, "branch", "--show-current").Out.Trim();
GitIn(cloneA, "push", "-q", "-u", "origin", branch);
GitIn(cloneB, "fetch", "-q", "origin");
GitIn(cloneB, "checkout", "-q", "-B", branch, "--track", $"origin/{branch}");

var csB = new GitBusTransport(cloneB, "relay", "cs-terminal");
csB.Send(Message.New("cs-terminal", null, "broadcast over the remote from C#"));
var recvA = Py(cloneA, "py-remote", "recv", "--from-start", "--exclude-self");
Check(recvA.Code == 0 && recvA.Out.Contains("broadcast over the remote from C#"),
	"python (clone A) receives the C# message pushed via the remote");

var sendA = Py(cloneA, "py-remote", "send", "--to", "cs-terminal", "remote reply from python");
Check(sendA.Code == 0, "python send over the remote succeeds");
Check(csB.Recv().Any(m => m.Body == "remote reply from python" && m.From == "py-remote"),
	"C# (clone B) pulls and sees the python message");

// ========== Scenario 3: forced concurrent-push race → union merge ==========
Console.WriteLine("\n== Scenario 3: concurrent appends — push rejected → rebase → union merge ==");
// Stage the race exactly at the seam Send() protects: C# has pulled and
// committed but NOT pushed; python lands a message on the remote first.
csB.PullRebase();
var mRace = Message.New("cs-terminal", null, "C# side of the race");
csB.AppendAndCommit(mRace);                     // committed locally, unpushed
var pyRace = Py(cloneA, "py-remote", "send", "python side of the race");
Check(pyRace.Code == 0, "python message lands on the remote first");
csB.PushWithRetry();                            // first push MUST be rejected → pull --rebase (union) → push
var afterRace = csB.Recv();
Check(afterRace.Any(m => m.Body == "C# side of the race") && afterRace.Any(m => m.Body == "python side of the race"),
	"after the race, C# sees BOTH messages (union merge kept both lines)");
var recvRaceA = Py(cloneA, "py-remote", "recv", "--from-start");
Check(recvRaceA.Out.Contains("C# side of the race") && recvRaceA.Out.Contains("python side of the race"),
	"python sees BOTH messages too — no line lost, no wedged rebase");
int idCount = File.ReadAllLines(Path.Combine(cloneB, "relay", "chat.jsonl")).Count(l => l.Trim().Length > 0);
var distinct = csB.Recv().Select(m => m.Id).Distinct().Count();
Check(distinct == csB.Recv().Count, $"no duplicate ids after the merge ({distinct} unique of {idCount} lines)");

// =========================== verdict =======================================
Console.WriteLine($"\n{passed} passed, {failed} failed");
return failed == 0 ? 0 : 1;
