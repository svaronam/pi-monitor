plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.sergio.pimonitor"
    compileSdk = 35
    defaultConfig {
        applicationId = "com.sergio.pimonitor"
        minSdk = 26
        targetSdk = 35
        versionCode = 2
        versionName = "2.1.0"
    }
    // Clave fija: así cada APK que compila GitHub se instala encima de la anterior sin desinstalar.
    // (Sin esto, cada compilación usa una clave de depuración nueva y Android rechaza la actualización.)
    signingConfigs {
        create("pimonitor") {
            storeFile = file("pimonitor.keystore")
            storePassword = System.getenv("KEYSTORE_PASSWORD")
            keyAlias = "pimonitor"
            keyPassword = System.getenv("KEYSTORE_PASSWORD")
        }
    }
    buildTypes {
        getByName("debug") { signingConfig = signingConfigs.getByName("pimonitor") }
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("pimonitor")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")
    implementation("com.google.android.material:material:1.12.0")
}
