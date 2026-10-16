// depotctl fallback build entry point, used when depotctl.local.toml has builder = "editor".
// Install with `depotctl install-build-script <project>` (copies this to Assets/Editor/Depotctl/).
//
// depotctl runs:
//   Unity -batchmode -nographics -quit -projectPath <p> -buildTarget Win64|OSXUniversal
//         -executeMethod Depotctl.DepotctlBuild.Build
//         -depotctlOutput <path to .exe or .app> [-depotctlMacArch universal|x64|arm64]
#if UNITY_EDITOR
using System;
using System.Linq;
using UnityEditor;
using UnityEditor.Build.Reporting;
using UnityEngine;

namespace Depotctl
{
    public static class DepotctlBuild
    {
        public static void Build()
        {
            try
            {
                var output = Arg("-depotctlOutput");
                if (string.IsNullOrEmpty(output))
                    throw new ArgumentException("missing -depotctlOutput <path>");

                var target = EditorUserBuildSettings.activeBuildTarget; // set by -buildTarget
                var scenes = EditorBuildSettings.scenes.Where(s => s.enabled).Select(s => s.path).ToArray();
                if (scenes.Length == 0)
                    throw new InvalidOperationException("no enabled scenes in Build Settings");

                if (target == BuildTarget.StandaloneOSX)
                    SetMacArchitecture(Arg("-depotctlMacArch") ?? "universal");

                var options = new BuildPlayerOptions
                {
                    scenes = scenes,
                    locationPathName = output,
                    target = target,
                    targetGroup = BuildPipeline.GetBuildTargetGroup(target),
                    options = BuildOptions.None,
                };

                Debug.Log($"[depotctl] building {target} -> {output} ({scenes.Length} scenes)");
                var report = BuildPipeline.BuildPlayer(options);
                var summary = report.summary;
                Debug.Log($"[depotctl] result {summary.result}, {summary.totalErrors} errors, " +
                          $"{summary.totalSize / (1024 * 1024)} MB, {summary.totalTime}");
                EditorApplication.Exit(summary.result == BuildResult.Succeeded ? 0 : 1);
            }
            catch (Exception e)
            {
                Debug.LogError($"[depotctl] build failed: {e}");
                EditorApplication.Exit(1);
            }
        }

        static void SetMacArchitecture(string arch)
        {
            UnityEditor.OSXStandalone.UserBuildSettings.architecture = arch switch
            {
                "x64" => UnityEditor.Build.OSArchitecture.x64,
                "arm64" => UnityEditor.Build.OSArchitecture.ARM64,
                _ => UnityEditor.Build.OSArchitecture.x64ARM64,
            };
        }

        static string Arg(string name)
        {
            var args = Environment.GetCommandLineArgs();
            var i = Array.IndexOf(args, name);
            return i >= 0 && i + 1 < args.Length ? args[i + 1] : null;
        }
    }
}
#endif
