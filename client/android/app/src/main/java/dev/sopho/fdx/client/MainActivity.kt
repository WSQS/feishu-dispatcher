package dev.sopho.fdx.client

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.wrapContentSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.tooling.preview.Preview
import dev.sopho.fdx.client.ui.theme.FdxViewerTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            FdxViewerTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background,
                ) {
                    Hello()
                }
            }
        }
    }
}

@Composable
private fun Hello() {
    Text(
        text = stringResource(R.string.hello),
        modifier = Modifier
            .fillMaxSize()
            .wrapContentSize(Alignment.Center),
    )
}

@Preview(showBackground = true)
@Composable
private fun HelloPreview() {
    FdxViewerTheme {
        Hello()
    }
}
