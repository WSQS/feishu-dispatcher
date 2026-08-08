package dev.sopho.fdx.client.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable

// Material3 配色。骨架阶段用默认紫调；正式 UI 再细调（#123 配置页时）。
private val DarkColors = darkColorScheme()
private val LightColors = lightColorScheme()

@Composable
fun FdxViewerTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    MaterialTheme(
        colorScheme = if (darkTheme) DarkColors else LightColors,
        content = content,
    )
}
