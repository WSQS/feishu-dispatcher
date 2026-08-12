package dev.sopho.fdx.client.ui

import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.assertHeightIsAtLeast
import androidx.compose.ui.test.assertWidthIsEqualTo
import androidx.compose.ui.test.click
import androidx.compose.ui.test.hasClickAction
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.unit.dp
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
    fun `file_row_click_target_spans_full_width_and_meets_minimum_height`() {
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
                    modifier = Modifier.width(320.dp),
                    onFileClick = { clickedPath = it },
                )
            }
            composeRule.waitUntil(timeoutMillis = 5_000) {
                composeRule.onAllNodes(hasText("dir/a.txt")).fetchSemanticsNodes().isNotEmpty()
            }

            val fileRow = composeRule.onNode(hasText("dir/a.txt") and hasClickAction())
            fileRow.assertWidthIsEqualTo(288.dp)
            fileRow.assertHeightIsAtLeast(48.dp)
            fileRow.performTouchInput { click(centerRight) }
            composeRule.runOnIdle {
                assertEquals("dir/a.txt", clickedPath)
            }
        } finally {
            client.close()
        }
    }
}
