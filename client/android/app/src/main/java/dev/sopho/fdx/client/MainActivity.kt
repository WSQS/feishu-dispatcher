package dev.sopho.fdx.client

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.navigation.toRoute
import dev.sopho.fdx.client.data.ConnectionStore
import dev.sopho.fdx.client.ui.ConfigScreen
import dev.sopho.fdx.client.ui.FileContentScreen
import dev.sopho.fdx.client.ui.ProjectListScreen
import dev.sopho.fdx.client.ui.TreeScreen
import dev.sopho.fdx.client.ui.theme.FdxViewerTheme
import kotlinx.serialization.Serializable

/**
 * 导航目的地：@Serializable 类型安全路由（Compose Navigation）。
 *
 * - [Config]：配置/连接页（栈底，根屏）。
 * - [ProjectList]：项目列表（连接成功后进入）。
 * - [Tree]：某项目的文件树（点项目进入，带 projectName）。
 * - [FileContent]：文件内容（点树里文件进入，带 projectName + path）。
 *
 * 后续加 Diff 屏时，在这里加一个子类即可。
 */
sealed class Destination {
    @Serializable
    data object Config : Destination()

    @Serializable
    data object ProjectList : Destination()

    @Serializable
    data class Tree(val projectName: String) : Destination()

    @Serializable
    data class FileContent(val projectName: String, val path: String) : Destination()
}

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = ConnectionStore(applicationContext)
        setContent {
            FdxViewerTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    // 连接（ViewerClient）在 Activity scope 的 ConnectionViewModel：跨配置变更存活，
                    // 进程死亡仍丢失（下方退回 Config 重连）。
                    val connVm: ConnectionViewModel = viewModel()
                    val navController = rememberNavController()
                    val storagePath = applicationContext.filesDir.absolutePath

                    NavHost(
                        navController = navController,
                        startDestination = Destination.Config,
                        modifier = Modifier.fillMaxSize(),
                    ) {
                        composable<Destination.Config> {
                            ConfigScreen(
                                repo = store,
                                storagePath = storagePath,
                                // onConnected 接收已建好并测过 health 的 client（复用，不再重建）。
                                onConnected = { c ->
                                    connVm.connect(c)
                                    // launchSingleTop：同目的地已在栈顶时不重复压栈（原 push 的去重语义）。
                                    navController.navigate(Destination.ProjectList) { launchSingleTop = true }
                                },
                            )
                        }
                        composable<Destination.ProjectList> {
                            val c = connVm.client
                            if (c == null) {
                                // 进程死亡重建后连接丢失（ConnectionViewModel 不跨进程），退回 Config 重连。
                                LaunchedEffect(Unit) { navController.popBackStack() }
                            } else {
                                ProjectListScreen(
                                    client = c,
                                    onProjectClick = { name ->
                                        // launchSingleTop：连点同一项目不压重复的 Tree。
                                        navController.navigate(Destination.Tree(name)) { launchSingleTop = true }
                                    },
                                )
                            }
                        }
                        composable<Destination.Tree> { entry ->
                            val route = entry.toRoute<Destination.Tree>()
                            val c = connVm.client
                            if (c == null) {
                                // 同 ProjectList：重建后无连接则退回。
                                LaunchedEffect(Unit) { navController.popBackStack() }
                            } else {
                                TreeScreen(
                                    client = c,
                                    projectName = route.projectName,
                                    onFileClick = { path ->
                                        navController.navigate(
                                            Destination.FileContent(route.projectName, path),
                                        ) { launchSingleTop = true }
                                    },
                                )
                            }
                        }
                        composable<Destination.FileContent> { entry ->
                            val route = entry.toRoute<Destination.FileContent>()
                            val c = connVm.client
                            if (c == null) {
                                LaunchedEffect(Unit) { navController.popBackStack() }
                            } else {
                                FileContentScreen(
                                    client = c,
                                    projectName = route.projectName,
                                    path = route.path,
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}
