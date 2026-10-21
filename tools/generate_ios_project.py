"""Deterministic Xcode project. No XcodeGen, package downloads or signing credentials."""
from pathlib import Path
import hashlib
ROOT = Path(__file__).resolve().parents[1]
def ident(name): return hashlib.sha1(name.encode()).hexdigest()[:24].upper()
def quoted(s): return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
def generate():
    objects = []
    def obj(name, body): objects.append(f'{ident(name)} = {{ {body} }};'); return ident(name)
    sources = ['SentryBridgeApp.swift', 'BridgeView.swift', 'BridgeModel.swift', 'BridgeWorker.swift', 'EFBDiscovery.swift', 'ReceiverReadings.swift', 'BridgeIntents.swift', 'DemoReplayer.swift', 'Licenses.swift', 'BridgeIO.c']
    refs, builds = [], []
    for name in sources + ['BridgeIO.h', 'Bridge-Bridging-Header.h', 'Info.plist']:
        kind = 'sourcecode.swift' if name.endswith('.swift') else 'sourcecode.c.c' if name.endswith('.c') else 'sourcecode.c.h' if name.endswith('.h') else 'text.plist.xml'
        refs.append(obj(name, f'isa = PBXFileReference; lastKnownFileType = {kind}; path = {quoted("SentryBridge/" + name)}; sourceTree = "<group>";'))
        if name in sources: builds.append(obj(name+'build', f'isa = PBXBuildFile; fileRef = {ident(name)};'))
    for name, path, kind in [('core', '../core/sbcore.c', 'sourcecode.c.c'), ('appicon', 'SentryBridge/AppIcon.icon', 'folder.iconcomposer.icon'), ('privacy', 'SentryBridge/PrivacyInfo.xcprivacy', 'text.xml'), ('demo', 'SentryBridge/DemoReceiver.bin', 'file'), ('notice', '../NOTICE.txt', 'text'), ('license', '../LICENSE', 'text')]:
        refs.append(obj(name, f'isa = PBXFileReference; lastKnownFileType = {kind}; path = {quoted(path)}; sourceTree = "<group>";'))
        obj(name+'build', f'isa = PBXBuildFile; fileRef = {ident(name)};')
    builds.append(ident('corebuild'))
    # Link AppIntents explicitly: Xcode extracts intent metadata only for a declared dependency.
    refs.append(obj('appintents', 'isa = PBXFileReference; lastKnownFileType = wrapper.framework; name = AppIntents.framework; path = System/Library/Frameworks/AppIntents.framework; sourceTree = SDKROOT;'))
    obj('appintentsbuild', f'isa = PBXBuildFile; fileRef = {ident("appintents")};')
    product = obj('product', 'isa = PBXFileReference; explicitFileType = wrapper.application; path = SentryBridge.app; sourceTree = BUILT_PRODUCTS_DIR;')
    products = obj('products', f'isa = PBXGroup; children = ({product},); name = Products; sourceTree = "<group>";')
    group = obj('group', f'isa = PBXGroup; children = ({",".join(refs + [products])},); sourceTree = "<group>";')
    sourcephase = obj('sources', f'isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = ({",".join(builds)},); runOnlyForDeploymentPostprocessing = 0;')
    resourcephase = obj('resources', f'isa = PBXResourcesBuildPhase; buildActionMask = 2147483647; files = ({ident("appiconbuild")},{ident("privacybuild")},{ident("demobuild")},{ident("noticebuild")},{ident("licensebuild")},); runOnlyForDeploymentPostprocessing = 0;')
    frameworks = obj('frameworks', f'isa = PBXFrameworksBuildPhase; buildActionMask = 2147483647; files = ({ident("appintentsbuild")},); runOnlyForDeploymentPostprocessing = 0;')
    common = {'SDKROOT':'iphoneos', 'IPHONEOS_DEPLOYMENT_TARGET':'26.0', 'SWIFT_VERSION':'5.0', 'CLANG_ENABLE_MODULES':'YES', 'GCC_C_LANGUAGE_STANDARD':'gnu11', 'TARGETED_DEVICE_FAMILY':'1,2'}
    app = {'PRODUCT_BUNDLE_IDENTIFIER':'io.github.tehcnological.sentrybridge', 'PRODUCT_NAME':'SentryBridge', 'INFOPLIST_FILE':'SentryBridge/Info.plist', 'SWIFT_OBJC_BRIDGING_HEADER':'SentryBridge/Bridge-Bridging-Header.h', 'ASSETCATALOG_COMPILER_APPICON_NAME':'AppIcon', 'HEADER_SEARCH_PATHS':'$(SRCROOT)/../core', 'CODE_SIGN_STYLE':'Automatic', 'GENERATE_INFOPLIST_FILE':'NO', 'SUPPORTED_PLATFORMS':'iphoneos iphonesimulator', 'SUPPORTS_MACCATALYST':'NO'}
    for scope in ('project','target'):
        configs=[]
        for mode in ('Debug','Release'):
            settings = dict(common)
            if scope=='target': settings.update(app)
            settings.update({'SWIFT_OPTIMIZATION_LEVEL':'-Onone' if mode=='Debug' else '-O', 'SWIFT_ACTIVE_COMPILATION_CONDITIONS':'DEBUG' if mode=='Debug' else '', 'GCC_OPTIMIZATION_LEVEL':'0' if mode=='Debug' else 's', 'DEBUG_INFORMATION_FORMAT':'dwarf' if mode=='Debug' else 'dwarf-with-dsym'})
            configs.append(obj(scope+mode, 'isa = XCBuildConfiguration; buildSettings = {' + ''.join(f'{k} = {quoted(v)};' for k,v in settings.items()) + f'}}; name = {mode};'))
        obj(scope+'configs', f'isa = XCConfigurationList; buildConfigurations = ({",".join(configs)},); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;')
    target = obj('target', f'isa = PBXNativeTarget; buildConfigurationList = {ident("targetconfigs")}; buildPhases = ({sourcephase},{frameworks},{resourcephase},); buildRules = (); dependencies = (); name = SentryBridge; productName = SentryBridge; productReference = {product}; productType = "com.apple.product-type.application";')
    uiFile = obj('uiFile', 'isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = tests/BridgeUITests.swift; sourceTree = "<group>";')
    uiBuild = obj('uiBuild', f'isa = PBXBuildFile; fileRef = {uiFile};')
    uiProduct = obj('uiProduct', 'isa = PBXFileReference; explicitFileType = wrapper.cfbundle; path = BridgeUITests.xctest; sourceTree = BUILT_PRODUCTS_DIR;')
    uiSources = obj('uiSources', f'isa = PBXSourcesBuildPhase; buildActionMask = 2147483647; files = ({uiBuild},); runOnlyForDeploymentPostprocessing = 0;')
    proxy = obj('uiProxy', f'isa = PBXContainerItemProxy; containerPortal = {ident("project")}; proxyType = 1; remoteGlobalIDString = {target}; remoteInfo = SentryBridge;')
    dependency = obj('uiDependency', f'isa = PBXTargetDependency; target = {target}; targetProxy = {proxy};')
    uiConfigs = []
    for mode in ('Debug','Release'):
        settings = dict(common, PRODUCT_BUNDLE_IDENTIFIER='io.github.tehcnological.sentrybridge.uitests', PRODUCT_NAME='BridgeUITests', GENERATE_INFOPLIST_FILE='YES', CODE_SIGN_STYLE='Automatic', TEST_TARGET_NAME='SentryBridge')
        uiConfigs.append(obj('ui'+mode, 'isa = XCBuildConfiguration; buildSettings = {' + ''.join(f'{k} = {quoted(v)};' for k,v in settings.items()) + f'}}; name = {mode};'))
    uiConfigList = obj('uiConfigs', f'isa = XCConfigurationList; buildConfigurations = ({",".join(uiConfigs)},); defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;')
    uiTarget = obj('uiTarget', f'isa = PBXNativeTarget; buildConfigurationList = {uiConfigList}; buildPhases = ({uiSources},); buildRules = (); dependencies = ({dependency},); name = BridgeUITests; productName = BridgeUITests; productReference = {uiProduct}; productType = "com.apple.product-type.bundle.ui-testing";')
    # Include tests in the navigator and Products group.
    objects = [entry.replace(f'children = ({",".join(refs + [products])},)', f'children = ({",".join(refs + [uiFile, products])},)').replace(f'children = ({product},)', f'children = ({product},{uiProduct},)') for entry in objects]
    project = obj('project', f'isa = PBXProject; attributes = {{LastUpgradeCheck = 2700;}}; buildConfigurationList = {ident("projectconfigs")}; compatibilityVersion = "Xcode 14.0"; developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en,Base,); mainGroup = {group}; productRefGroup = {products}; projectDirPath = ""; projectRoot = ""; targets = ({target},{uiTarget},);')
    path=ROOT/'ios/SentryBridge.xcodeproj'; path.mkdir(exist_ok=True)
    (path/'project.pbxproj').write_text('// !$*UTF8*$!\n{archiveVersion = 1; classes = {}; objectVersion = 56; objects = {\n'+'\n'.join(objects)+f'\n}}; rootObject = {project};}}\n')
    scheme=path/'xcshareddata/xcschemes/SentryBridge.xcscheme'; scheme.parent.mkdir(parents=True,exist_ok=True)
    reference=f'<BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{target}" BuildableName="SentryBridge.app" BlueprintName="SentryBridge" ReferencedContainer="container:SentryBridge.xcodeproj"/>'
    testReference = f'<BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{uiTarget}" BuildableName="BridgeUITests.xctest" BlueprintName="BridgeUITests" ReferencedContainer="container:SentryBridge.xcodeproj"/>'
    scheme.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion="2600" version="1.3"><BuildAction parallelizeBuildables="YES" buildImplicitDependencies="YES"><BuildActionEntries><BuildActionEntry buildForTesting="YES" buildForRunning="YES" buildForProfiling="YES" buildForArchiving="YES" buildForAnalyzing="YES">{reference}</BuildActionEntry></BuildActionEntries></BuildAction><TestAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" shouldUseLaunchSchemeArgsEnv="YES"><Testables><TestableReference skipped="NO">{testReference}</TestableReference></Testables></TestAction><LaunchAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" launchStyle="0" useCustomWorkingDirectory="NO" ignoresPersistentStateOnLaunch="NO" debugDocumentVersioning="YES" allowLocationSimulation="NO"><BuildableProductRunnable runnableDebuggingMode="0">{reference}</BuildableProductRunnable></LaunchAction><ProfileAction buildConfiguration="Release" shouldUseLaunchSchemeArgsEnv="YES" savedToolIdentifier="" useCustomWorkingDirectory="NO" debugDocumentVersioning="YES"><BuildableProductRunnable runnableDebuggingMode="0">{reference}</BuildableProductRunnable></ProfileAction><AnalyzeAction buildConfiguration="Debug"/><ArchiveAction buildConfiguration="Release" revealArchiveInOrganizer="YES"/></Scheme>
''')
if __name__=='__main__': generate()
