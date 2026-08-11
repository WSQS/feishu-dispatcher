package dev.sopho.fdx.client.ui

import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import kotlin.test.Test
import kotlin.test.assertEquals
import org.junit.Rule
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

@RunWith(AndroidJUnit4::class)
@Config(sdk = [35])
class TreeScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun `file_click_passes_entry_path_to_callback`() {
        val engine = MockEngine {
            respond(
                """{"entries":[{"path":"dir/a.txt","type":"file","size":10}]}""",
                HttpStatusCode.OK,
                headersOf("Content-Type", "application/json"),
            )
        }
        val client = mockClient(engine)
        var clickedPath: String? = null

        try {
            composeRule.setContent {
                TreeScreen(
                    client = client,
                    projectName = "proj",
                    onFileClick = { clickedPath = it },
                )
            }
            composeRule.waitUntil(timeoutMillis = 5_000) {
                composeRule.onAllNodes(hasText("dir/a.txt")).fetchSemanticsNodes().isNotEmpty()
            }

            composeRule.onNodeWithText("dir/a.txt").performClick()
            composeRule.runOnIdle {
                assertEquals("dir/a.txt", clickedPath)
            }
        } finally {
            client.close()
        }
    }
}
